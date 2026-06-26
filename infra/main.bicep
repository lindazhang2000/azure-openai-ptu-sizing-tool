targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the azd environment; used to derive resource names and tags.')
param environmentName string

@minLength(1)
@description('Primary Azure region for all resources.')
param location string

@description('Optional explicit resource group name. Defaults to rg-<environmentName>.')
param resourceGroupName string = ''

var tags = { 'azd-env-name': environmentName }
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var rgName = !empty(resourceGroupName) ? resourceGroupName : 'rg-${environmentName}'

resource rg 'Microsoft.Resources/resourceGroups@2022-09-01' = {
  name: rgName
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
  }
}

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.AZURE_CONTAINER_REGISTRY_ENDPOINT
output AZURE_LOCATION string = location
output WEB_URI string = resources.outputs.WEB_URI
