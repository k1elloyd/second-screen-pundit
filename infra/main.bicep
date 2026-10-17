// Second Screen Pundit: one container app, its registry, identity and monitoring.
// Deploys into the resource group azd targets (AZURE_RESOURCE_GROUP, rg-<environment name>);
// it never creates a resource group. The Foundry account and the Speech resource already exist
// and are referenced, not created.
targetScope = 'resourceGroup'

@minLength(1)
@maxLength(64)
@description('azd environment name; used for naming and the azd-env-name tag.')
param environmentName string

@description('Region for everything this template creates. Not taken from AZURE_LOCATION.')
param location string = 'uksouth'

@description('Existing Foundry account that holds the model deployment (B7).')
param existingFoundryAccountName string
param existingFoundryResourceGroup string
@description('Foundry project on that account.')
param foundryProjectName string
@description('Model deployment names on that account; both point at gpt-5.4-mini.')
param modelFast string = 'gpt-5.4-mini'
param modelDeep string = 'gpt-5.4-mini'

@description('Existing Speech resource in a MAI-voice region; referenced, not recreated.')
param existingSpeechAccountName string
@description('Empty means the resource group being deployed to.')
param existingSpeechResourceGroup string = ''
param speechVoice string = 'en-GB-Harry:MAI-Voice-2.1-Flash'
param speechFallbackVoice string = 'en-GB-RyanNeural'

@allowed(['live', 'cached', 'off'])
param narrativeMode string = 'live'

@minValue(0)
@maxValue(1)
@description('1 keeps one replica warm for judging; 0 scales to zero when idle.')
param minReplicas int = 1

@minValue(0)
@description('Global daily cap on model calls (UTC day). 0 sends every new viewer to cached mode.')
param modelCallsPerDay int = 1500

@minValue(0)
@description('Global daily cap on text-to-speech characters (UTC day).')
param ttsCharactersPerDay int = 150000

@description('Image set by azd deploy (SERVICE_API_IMAGE_NAME); empty on the first provision.')
param apiImageName string = ''

// In-memory state (streams, outboxes, replays, pause, resume positions) lives in one process,
// so a second replica would break resume. Never more than one.
var maxReplicas = 1
var resourceToken = toLower(uniqueString(resourceGroup().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }
var usePlaceholder = empty(apiImageName)
// Always 8000: azd deploy swaps only the image and keeps the port, so the port must be the
// real app's from the first provision. The placeholder (port 80) is never healthy; it only
// holds the app until azd deploys the real image, and has no custom probes.
var appPort = 8000
var speechResourceGroup = empty(existingSpeechResourceGroup)
  ? resourceGroup().name
  : existingSpeechResourceGroup

// Role definition GUIDs, checked on Learn's built-in roles pages.
var roles = {
  foundryUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  speechUser: 'f2dc8367-1007-4938-bd23-fe263f013447'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
}

// ---- Existing resources (fail the deployment early if any is missing) ----

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: existingFoundryAccountName
  scope: resourceGroup(existingFoundryResourceGroup)
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundryAccount
  name: foundryProjectName
}

resource speechAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: existingSpeechAccountName
  scope: resourceGroup(speechResourceGroup)
}

// ---- Identity ----

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-pundit-${resourceToken}'
  location: location
  tags: tags
}

module foundryRole 'modules/cognitive-role.bicep' = {
  name: 'foundry-user-${resourceToken}'
  scope: resourceGroup(existingFoundryResourceGroup)
  params: {
    accountName: foundryAccount.name
    principalId: identity.properties.principalId
    roleDefinitionId: roles.foundryUser
  }
}

module speechRole 'modules/cognitive-role.bicep' = {
  name: 'speech-user-${resourceToken}'
  scope: resourceGroup(speechResourceGroup)
  params: {
    accountName: speechAccount.name
    principalId: identity.properties.principalId
    roleDefinitionId: roles.speechUser
  }
}

// ---- Registry (azd builds the image remotely with ACR Tasks; no local Docker) ----

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'crpundit${resourceToken}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, roles.acrPull)
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}

// ---- Monitoring (keyless: no shared keys, no local auth) ----

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-pundit-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-pundit-${resourceToken}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    // Entra-only ingestion: the connection string alone can't send telemetry.
    DisableLocalAuth: true
  }
}

resource metricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, identity.id, roles.monitoringMetricsPublisher)
  scope: appInsights
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.monitoringMetricsPublisher
    )
  }
}

// ---- Container Apps ----

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: 'cae-pundit-${resourceToken}'
  location: location
  tags: tags
  properties: {
    // Logs go through Azure Monitor diagnostic settings, so the environment holds no
    // Log Analytics shared key.
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
  }
}

resource environmentLogs 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'logs-to-workspace'
  scope: environment
  properties: {
    workspaceId: workspace.id
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
  }
}

var healthProbes = [
  {
    // Import and warm-up take about 15 s locally; allow 60 s. /healthz makes no model or
    // speech calls and answers 200 while warming, so the page can show "Warming up".
    type: 'Startup'
    httpGet: { path: '/healthz', port: appPort }
    initialDelaySeconds: 3
    periodSeconds: 3
    failureThreshold: 20
    timeoutSeconds: 3
  }
  {
    type: 'Liveness'
    httpGet: { path: '/healthz', port: appPort }
    periodSeconds: 30
    failureThreshold: 3
    timeoutSeconds: 5
  }
  {
    type: 'Readiness'
    httpGet: { path: '/healthz', port: appPort }
    periodSeconds: 10
    failureThreshold: 3
    timeoutSeconds: 5
  }
]

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: 'ca-pundit-${resourceToken}'
  location: location
  tags: union(tags, { 'azd-service-name': 'api' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: appPort
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: usePlaceholder ? 'mcr.microsoft.com/k8se/quickstart:latest' : apiImageName
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          // Plain settings only: no secrets, no keys. Access is by the managed identity.
          env: [
            { name: 'NARRATIVE_MODE', value: narrativeMode }
            { name: 'MODEL_CALLS_PER_DAY', value: string(modelCallsPerDay) }
            { name: 'TTS_CHARACTERS_PER_DAY', value: string(ttsCharactersPerDay) }
            {
              name: 'FOUNDRY_PROJECT_ENDPOINT'
              value: 'https://${foundryAccount.properties.customSubDomainName}.services.ai.azure.com/api/projects/${foundryProject.name}'
            }
            { name: 'MODEL_FAST', value: modelFast }
            { name: 'MODEL_DEEP', value: modelDeep }
            { name: 'SPEECH_ENDPOINT', value: speechAccount.properties.endpoint }
            { name: 'SPEECH_REGION', value: speechAccount.location }
            { name: 'SPEECH_VOICE', value: speechVoice }
            { name: 'SPEECH_FALLBACK_VOICE', value: speechFallbackVoice }
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
            { name: 'OTEL_SERVICE_NAME', value: 'second-screen-pundit' }
            { name: 'ENABLE_SENSITIVE_DATA', value: 'false' }
            // No request spans for probes, audio downloads or the SSE stream: the stream would
            // add one "http send" span per message, about 1,500 per match.
            {
              name: 'OTEL_PYTHON_FASTAPI_EXCLUDED_URLS'
              value: 'healthz,api/replays/[^/]+/stream,api/audio/'
            }
            { name: 'PORT', value: '8000' }
          ]
          probes: usePlaceholder ? [] : healthProbes
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
  dependsOn: [
    acrPull
    metricsPublisher
    foundryRole
    speechRole
  ]
}

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.properties.loginServer
output AZURE_CONTAINER_APPS_ENVIRONMENT_NAME string = environment.name
output SERVICE_API_NAME string = app.name
output SERVICE_API_URI string = 'https://${app.properties.configuration.ingress.fqdn}'
output APPLICATIONINSIGHTS_NAME string = appInsights.name
output AZURE_MANAGED_IDENTITY_CLIENT_ID string = identity.properties.clientId
