# KB-102: Admin portal sign-in fails with AADSTS700027 (expired SSO signing certificate)

**Product:** OrderSync admin portal, v4.0 and later | **Category:** Identity and sign-in | **Last reviewed:** August 2026

## Symptoms
- No admin can sign in to the OrderSync admin portal.
- The error is `AADSTS700027: Client assertion contains an invalid signature`.
- Order sync keeps running, because only the admin portal uses SSO.

## Cause
The admin portal signs its sign-in requests with the certificate `ordersync-admin-sso`. When that certificate expires and the automatic rotation job is skipped (for example, because the job's own credentials expired), Microsoft Entra ID rejects every sign-in request.

Look for this log line: `Certificate rotation job skipped: signing certificate 'ordersync-admin-sso' expired`.

## Resolution
1. In the OrderSync tenant settings, go to **Security > SSO certificates** and select **Rotate now**.
2. Upload the new public certificate to the customer's Entra ID app registration (**Certificates & secrets**). The customer's identity admin usually does this step.
3. Wait up to 10 minutes, then retry sign-in.
4. Re-enable the rotation job so it runs 30 days before expiry.

## Severity guidance
Usually **Sev2**: admins are blocked, but order processing isn't affected. See KB-107.

## Related
KB-107 (severity definitions)
