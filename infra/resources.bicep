// Everything the sample needs, in one resource group. See docs/architecture.md for how the pieces connect.

param location string
param searchLocation string
param tags object
param principalId string
param principalType string
param chatModel object
param triageModel object
param embeddingModel object
param functionAlwaysReady int
param supportEngineerName string

var token = toLower(uniqueString(subscription().id, resourceGroup().id, location))

// Built-in role definition IDs
var roles = {
  foundryUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d' // Azure AI User (shown as Foundry User in the portal)
  cognitiveServicesUser: 'a97b65f3-24c7-4388-baec-2e87135dc908'
  blobDataReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
  blobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  blobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  queueDataContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  tableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
}

// ---------- Monitoring ----------

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${token}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${token}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

// ---------- Microsoft Foundry: account, project, models, guardrail ----------

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'aif-${token}'
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    allowProjectManagement: true
    customSubDomainName: 'aif-${token}'
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true // Entra ID only: every caller in this sample uses a managed identity or your sign-in
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: 'support-agents'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Contoso support agents'
    description: 'AI customer support agent: a hosted Case Manager that runs five prompt agents with a human approval gate.'
  }
}

// The project and the deployments are created one at a time; the account rejects parallel changes.
resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: chatModel.name
  sku: { name: chatModel.sku, capacity: chatModel.capacity }
  properties: {
    model: { format: 'OpenAI', name: chatModel.name, version: chatModel.version }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
  dependsOn: [project]
}

resource triageDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: triageModel.name
  sku: { name: triageModel.sku, capacity: triageModel.capacity }
  properties: {
    model: { format: 'OpenAI', name: triageModel.name, version: triageModel.version }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
  dependsOn: [chatDeployment]
}

resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: embeddingModel.name
  sku: { name: embeddingModel.sku, capacity: embeddingModel.capacity }
  properties: {
    model: { format: 'OpenAI', name: embeddingModel.name, version: embeddingModel.version }
  }
  dependsOn: [triageDeployment]
}

// The guardrail every prompt agent uses (rai_config in agents/*/agent.json). Prompt shields block jailbreaks in
// the prompt and indirect attacks (instructions hidden in tickets, logs, or KB text), including in tool results.
var harmCategories = ['Hate', 'Sexual', 'Selfharm', 'Violence']
resource guardrail 'Microsoft.CognitiveServices/accounts/raiPolicies@2025-06-01' = {
  parent: account
  name: 'support-agents-guardrail'
  properties: {
    basePolicyName: 'Microsoft.DefaultV2'
    mode: 'Default'
    contentFilters: concat(
      flatten(map(harmCategories, c => [
        { name: c, blocking: true, enabled: true, severityThreshold: 'Medium', source: 'Prompt' }
        { name: c, blocking: true, enabled: true, severityThreshold: 'Medium', source: 'Completion' }
      ])),
      [
        { name: 'Jailbreak', blocking: true, enabled: true, source: 'Prompt' }
        { name: 'Indirect Attack', blocking: true, enabled: true, source: 'Prompt' }
        { name: 'Indirect Attack', blocking: true, enabled: true, source: 'PostToolCall' }
        { name: 'Protected Material Text', blocking: true, enabled: true, source: 'Completion' }
        { name: 'Protected Material Code', blocking: true, enabled: true, source: 'Completion' }
        { name: 'Task Adherence', blocking: false, enabled: true, source: 'PreToolCall' }
      ]
    )
  }
  dependsOn: [embeddingDeployment]
}

// Traces from every agent run land in Application Insights (Foundry portal > Tracing).
resource appInsightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: 'app-insights'
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: { key: appInsights.properties.ConnectionString }
    metadata: { ApiType: 'Azure', ResourceId: appInsights.id }
  }
}

// ---------- Azure AI Search: Foundry IQ knowledge base ----------

resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: 'srch-${token}'
  location: searchLocation
  tags: tags
  sku: { name: 'basic' }
  identity: { type: 'SystemAssigned' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    semanticSearch: 'free'
    authOptions: { aadOrApiKey: { aadAuthFailureMode: 'http401WithBearerChallenge' } }
  }
}

// ---------- Storage: tickets (tables), KB articles (blob), function packages (blob) ----------

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'st${token}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false // managed identities only
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'deployments'
}

resource kbContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'kb-articles'
}

// ---------- Ticket system: Azure Functions (Flex Consumption) exposing MCP tools ----------

resource functionIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-func-${token}'
  location: location
  tags: tags
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-${token}'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: { tier: 'FlexConsumption', name: 'FC1' }
  properties: { reserved: true }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-tickets-${token}'
  location: location
  tags: union(tags, { 'azd-service-name': 'ticket-system' })
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: { '${functionIdentity.id}': {} }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: { minTlsVersion: '1.2' }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer.name}'
          authentication: { type: 'UserAssignedIdentity', userAssignedIdentityResourceId: functionIdentity.id }
        }
      }
      scaleAndConcurrency: {
        alwaysReady: functionAlwaysReady > 0 ? [{ name: 'http', instanceCount: functionAlwaysReady }] : []
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: { name: 'python', version: '3.13' }
    }
  }

  resource settings 'config' = {
    name: 'appsettings'
    properties: {
      AzureWebJobsStorage__credential: 'managedidentity'
      AzureWebJobsStorage__clientId: functionIdentity.properties.clientId
      AzureWebJobsStorage__blobServiceUri: storage.properties.primaryEndpoints.blob
      AzureWebJobsStorage__queueServiceUri: storage.properties.primaryEndpoints.queue
      AzureWebJobsStorage__tableServiceUri: storage.properties.primaryEndpoints.table
      APPLICATIONINSIGHTS_CONNECTION_STRING: appInsights.properties.ConnectionString
      FOUNDRY_PROJECT_ENDPOINT: 'https://${account.properties.customSubDomainName}.services.ai.azure.com/api/projects/${project.name}'
      TICKETS_TABLE_ENDPOINT: storage.properties.primaryEndpoints.table
      CASE_INTAKE_ROUTINE: 'case-intake'
      SUPPORT_ENGINEER_NAME: supportEngineerName
    }
  }
}

// ---------- Role assignments (least privilege, no keys) ----------

// Foundry IQ: Search reads the KB blobs and calls the embedding and chat models with its own identity.
resource searchToStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, search.id, roles.blobDataReader)
  properties: { principalId: search.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataReader) }
}

resource searchToFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, search.id, roles.cognitiveServicesUser)
  properties: { principalId: search.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesUser) }
}

// Agents query the knowledge base through the project's identity.
resource projectToSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, project.id, roles.searchIndexDataReader)
  properties: { principalId: project.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader) }
}

resource projectToFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, 'project-mi', roles.foundryUser)
  properties: { principalId: project.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.foundryUser) }
}

// The ticket system stores tickets in Table Storage and starts Case Manager runs through a Foundry routine.
resource funcToTables 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, functionApp.id, roles.tableDataContributor)
  properties: { principalId: functionApp.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.tableDataContributor) }
}

resource funcToFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, functionApp.id, roles.foundryUser)
  properties: { principalId: functionApp.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.foundryUser) }
}

// The Functions host itself (packages, triggers, timers) uses the user-assigned identity.
resource hostRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in [roles.blobDataOwner, roles.queueDataContributor, roles.tableDataContributor]: {
  scope: storage
  name: guid(storage.id, functionIdentity.id, role)
  properties: { principalId: functionIdentity.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role) }
}]

// You (the deployer): build agents in the project, run evals, and let the setup script load the knowledge base.
resource userToFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  scope: account
  name: guid(account.id, principalId, roles.foundryUser)
  properties: { principalId: principalId, principalType: principalType, roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.foundryUser) }
}

resource userToSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in [roles.searchServiceContributor, roles.searchIndexDataContributor]: if (!empty(principalId)) {
  scope: search
  name: guid(search.id, principalId, role)
  properties: { principalId: principalId, principalType: principalType, roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role) }
}]

resource userToStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in [roles.blobDataContributor, roles.tableDataContributor]: if (!empty(principalId)) {
  scope: storage
  name: guid(storage.id, principalId, role)
  properties: { principalId: principalId, principalType: principalType, roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role) }
}]

output accountName string = account.name
output projectName string = project.name
output projectId string = project.id
output projectEndpoint string = 'https://${account.properties.customSubDomainName}.services.ai.azure.com/api/projects/${project.name}'
output openAiEndpoint string = 'https://${account.properties.customSubDomainName}.openai.azure.com'
output raiPolicyId string = guardrail.id
output searchName string = search.name
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output storageName string = storage.name
output storageId string = storage.id
output functionAppName string = functionApp.name
output functionAppHostName string = functionApp.properties.defaultHostName
output appInsightsConnectionString string = appInsights.properties.ConnectionString
