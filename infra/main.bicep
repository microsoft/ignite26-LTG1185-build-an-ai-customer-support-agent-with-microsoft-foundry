targetScope = 'subscription'

@minLength(1)
@maxLength(40)
@description('Name of the azd environment. Used to name the resource group and generate a unique suffix for resources.')
param environmentName string

@minLength(1)
@description('Region for all resources. The models below must be available here (East US 2 and Sweden Central are good choices).')
param location string

@description('Region for Azure AI Search. Defaults to the main region; change it if Search Basic has no capacity there.')
param searchLocation string = ''

@description('Object ID of the person or service principal running azd. Gets the data-plane roles the setup script needs.')
param principalId string = ''

@allowed(['User', 'ServicePrincipal'])
param principalType string = 'User'

@description('Chat model for the drafting, diagnostics, knowledge, and follow-up agents, Foundry IQ, and customer memory.')
param chatModel object = {
  name: 'gpt-5-mini'
  version: '2025-08-07'
  sku: 'GlobalStandard'
  capacity: 100
}

@description('Model for the triage agent and the evaluation judge.')
param triageModel object = {
  name: 'gpt-5.4-mini'
  version: '2026-03-17'
  sku: 'DataZoneStandard'
  capacity: 50
}

@description('Embedding model for Foundry IQ (knowledge base) and customer memory.')
param embeddingModel object = {
  name: 'text-embedding-3-small'
  version: '1'
  sku: 'GlobalStandard'
  capacity: 100
}

@description('Tokens-per-minute capacity (in thousands) for each model. Raise them if runs are throttled and you have quota.')
param chatModelCapacity string = '100'
param triageModelCapacity string = '50'
param embeddingModelCapacity string = '100'

@description('Always-ready instances for the ticket system. 1 avoids cold starts during demos; set 0 to pay only per use.')
param functionAlwaysReady string = '1'

@description('Name the support console uses for the signed-in engineer (the sample has no sign-in).')
param supportEngineerName string = 'Alex Rivera'

var tags = { 'azd-env-name': environmentName }

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    location: location
    searchLocation: empty(searchLocation) ? location : searchLocation
    tags: tags
    principalId: principalId
    principalType: principalType
    chatModel: union(chatModel, { capacity: int(chatModelCapacity) })
    triageModel: union(triageModel, { capacity: int(triageModelCapacity) })
    embeddingModel: union(embeddingModel, { capacity: int(embeddingModelCapacity) })
    functionAlwaysReady: int(functionAlwaysReady)
    supportEngineerName: supportEngineerName
  }
}

output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rg.name

output AZURE_AI_ACCOUNT_NAME string = resources.outputs.accountName
output AZURE_AI_PROJECT_NAME string = resources.outputs.projectName
output AZURE_AI_PROJECT_ID string = resources.outputs.projectId
output FOUNDRY_PROJECT_ENDPOINT string = resources.outputs.projectEndpoint
output AZURE_AI_PROJECT_ENDPOINT string = resources.outputs.projectEndpoint
output AZURE_OPENAI_ENDPOINT string = resources.outputs.openAiEndpoint
output CHAT_MODEL_DEPLOYMENT string = chatModel.name
output TRIAGE_MODEL_DEPLOYMENT string = triageModel.name
output EMBEDDING_MODEL_DEPLOYMENT string = embeddingModel.name
output RAI_POLICY_ID string = resources.outputs.raiPolicyId

output SEARCH_SERVICE_NAME string = resources.outputs.searchName
output SEARCH_ENDPOINT string = resources.outputs.searchEndpoint
output KNOWLEDGE_BASE_MCP_URL string = '${resources.outputs.searchEndpoint}/knowledgebases/support-kb/mcp?api-version=2026-08-01-preview'

output STORAGE_ACCOUNT_NAME string = resources.outputs.storageName
output STORAGE_ACCOUNT_ID string = resources.outputs.storageId

output FUNCTION_APP_NAME string = resources.outputs.functionAppName
output TICKET_SYSTEM_URL string = 'https://${resources.outputs.functionAppHostName}'
output TICKET_SYSTEM_MCP_URL string = 'https://${resources.outputs.functionAppHostName}/runtime/webhooks/mcp'
output KB_BASE_URL string = 'https://${resources.outputs.functionAppHostName}/api/kb'

output TOOLBOX_ENDPOINT string = '${resources.outputs.projectEndpoint}/toolboxes/support-toolbox/mcp?api-version=v1'
output APPLICATIONINSIGHTS_CONNECTION_STRING string = resources.outputs.appInsightsConnectionString
