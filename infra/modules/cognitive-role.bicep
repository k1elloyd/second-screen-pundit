// A role assignment on an existing Cognitive Services account (Foundry or Speech), scoped to
// that account, in whichever resource group holds it.
targetScope = 'resourceGroup'

param accountName string
param principalId string
@description('Role definition GUID; roles are assigned by GUID, not name.')
param roleDefinitionId string

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource assignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, roleDefinitionId)
  scope: account
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleDefinitionId)
  }
}
