<#
.SYNOPSIS
    Package and deploy the Streamlit PTU sizing app to Azure App Service.

.DESCRIPTION
    Zips the repo root (excluding local virtual envs, git metadata, and docs/CI
    folders) and deploys it to an existing Linux Python App Service using
    Oryx build (pip install from root requirements.txt). If the target App
    Service / plan / resource group do not exist, pass -Provision to create
    them (Free F1 tier, which uses shared compute and needs no VM quota).

.PARAMETER ResourceGroup
    Resource group name. Default: ptu-sizing-rg

.PARAMETER AppName
    Web app name (must be globally unique). Default: ptu-sizing-tool-lz

.PARAMETER PlanName
    App Service plan name. Default: ptu-plan-westus2

.PARAMETER Location
    Azure region used only when provisioning. Default: westus2

.PARAMETER Provision
    Create the resource group, plan (F1 Linux), and web app before deploying.

.EXAMPLE
    ./scripts/deploy-appservice.ps1
    Redeploy the current code to the existing App Service.

.EXAMPLE
    ./scripts/deploy-appservice.ps1 -Provision -AppName my-ptu-app
    Create a new Free-tier App Service and deploy to it.

.NOTES
    Requires Azure CLI and an active `az login` session.
    Run from the repository root.
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "ptu-sizing-rg",
    [string]$AppName       = "ptu-sizing-tool-lz",
    [string]$PlanName      = "ptu-plan-westus2",
    [string]$Location      = "westus2",
    [switch]$Provision
)

$ErrorActionPreference = "Stop"

# Resolve repo root as the parent of this script's folder.
$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepoRoot
try {
    Write-Host "Repo root: $RepoRoot" -ForegroundColor Cyan

    # Confirm Azure CLI is logged in.
    $account = az account show --query "name" -o tsv 2>$null
    if (-not $account) {
        throw "Not logged in to Azure CLI. Run 'az login' first."
    }
    Write-Host "Azure subscription: $account" -ForegroundColor Cyan

    if ($Provision) {
        Write-Host "Provisioning App Service (F1 Linux, Python 3.12)..." -ForegroundColor Yellow
        az group create -n $ResourceGroup -l $Location -o none
        az appservice plan create -g $ResourceGroup -n $PlanName --is-linux --sku F1 -o none
        az webapp create -g $ResourceGroup -p $PlanName -n $AppName --runtime "PYTHON:3.12" -o none

        # Build dependencies during deployment and set the Streamlit startup command.
        az webapp config appsettings set -g $ResourceGroup -n $AppName `
            --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true -o none
        az webapp config set -g $ResourceGroup -n $AppName --startup-file `
            "python -m streamlit run app/ptu_streamlit_app.py --server.port 8000 --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false" -o none
        Write-Host "Provisioned $AppName." -ForegroundColor Green
    }

    # --- Package the repo root into a deployment zip ---
    $zip = Join-Path $RepoRoot "deploy.zip"
    $tmp = Join-Path $RepoRoot "_deploy_pkg"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    New-Item -ItemType Directory $tmp | Out-Null

    # Local-only or non-runtime folders that should not ship to App Service.
    # Any folder named like a virtual env (.venv, .venv-1, .venv-2, ...) is skipped.
    $exclude = @('.git', '_deploy_pkg', 'deploy.zip', 'docs', 'linkedin',
                 '.github', '.playwright-mcp', '.azure', '.pytest_cache')

    Get-ChildItem -Force | Where-Object {
        $exclude -notcontains $_.Name -and $_.Name -notlike '.venv*'
    } | ForEach-Object {
        Copy-Item $_.FullName -Destination $tmp -Recurse -Force
    }
    Get-ChildItem $tmp -Recurse -Filter '__pycache__' |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path "$tmp\*" -DestinationPath $zip -Force
    Remove-Item $tmp -Recurse -Force
    Write-Host "Created package: $zip" -ForegroundColor Green

    # --- Deploy ---
    Write-Host "Deploying to $AppName..." -ForegroundColor Yellow
    az webapp deployment source config-zip -g $ResourceGroup -n $AppName --src $zip

    Remove-Item $zip -Force
    $hostName = az webapp show -g $ResourceGroup -n $AppName --query "defaultHostName" -o tsv
    Write-Host "Deployed. App URL: https://$hostName" -ForegroundColor Green
}
finally {
    Pop-Location
}
