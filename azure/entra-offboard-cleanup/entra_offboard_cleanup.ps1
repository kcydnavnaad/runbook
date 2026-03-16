# ============================================================
# Remove-OffboardedUsers.ps1
# Verwijdert Entra ID accounts die langer dan 30 dagen in de
# offboarded-users groep zitten.
#
# Omgevingsvariabelen (GitLab CI/CD variables):
#   AZURE_TENANT_ID      - Tenant ID van je Entra ID
#   AZURE_CLIENT_ID      - Client ID van de App Registration
#   AZURE_CLIENT_SECRET  - Client Secret van de App Registration
#   DRY_RUN              - "true" of "false" (standaard: "true")
#   SLACK_WEBHOOK_URL    - Slack Incoming Webhook URL
# ============================================================

#region CONFIG
$DaysBeforeDelete = 30
$LogDir           = "./logs"
$DryRun           = ($env:DRY_RUN -ne "false")
$TenantId         = $env:AZURE_TENANT_ID
$ClientId         = $env:AZURE_CLIENT_ID
$ClientSecret     = $env:AZURE_CLIENT_SECRET
$SlackWebhook     = $env:SLACK_WEBHOOK_URL
#endregion

#region VALIDATE ENV VARS
foreach ($var in @("AZURE_TENANT_ID","AZURE_CLIENT_ID","AZURE_CLIENT_SECRET")) {
    if (-not (Get-Item env:$var -ErrorAction SilentlyContinue)) {
        Write-Error "Omgevingsvariabele $var is niet ingesteld. Script wordt afgebroken."
        exit 1
    }
}
#endregion

#region SETUP LOGGING
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$LogFile = Join-Path $LogDir "offboard_$(Get-Date -Format 'yyyy-MM-dd_HHmm').log"

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line      = "[$timestamp] [$Level] $Message"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}
#endregion

#region SLACK
function Send-SlackMessage {
    param([string]$Text)
    if (-not $SlackWebhook) {
        Write-Log "SLACK_WEBHOOK_URL niet ingesteld, melding overgeslagen." "WARN"
        return
    }
    try {
        $body = @{ text = $Text } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri $SlackWebhook -Method Post -Body $body -ContentType "application/json" | Out-Null
    } catch {
        Write-Log "Slack melding mislukt: $_" "WARN"
    }
}
#endregion

#region CONNECT
Write-Log "Verbinding maken met Microsoft Graph via App Registration..."
try {
    $SecureSecret = ConvertTo-SecureString $ClientSecret -AsPlainText -Force
    $Credential   = New-Object System.Management.Automation.PSCredential($ClientId, $SecureSecret)
    Connect-MgGraph -TenantId $TenantId -ClientSecretCredential $Credential -ErrorAction Stop
    Write-Log "Verbonden met Microsoft Graph (Tenant: $TenantId)"
} catch {
    Write-Log "Verbinding mislukt: $_" "ERROR"
    Send-SlackMessage ":x: *m365-offboarding-cleanup* — Verbinding met Microsoft Graph mislukt: $_"
    exit 1
}
#endregion

#region MAIN LOGIC
$cutoffDate = (Get-Date).AddDays(-$DaysBeforeDelete)
$today      = Get-Date
$GroupName  = "offboarded-users"
$startTime  = Get-Date -Format "yyyy-MM-dd HH:mm"

Write-Log "Ophalen van leden uit groep '$GroupName'..."

# Lijsten per categorie
$toDelete     = @()   # In aanmerking voor verwijdering
$notYet       = @()   # In groep maar nog niet 30 dagen
$skippedSync  = @()   # Hybrid/synced accounts
$warnUsers    = @()   # Activiteit niet opgehaald
$deletedUsers = @()   # Effectief verwijderd
$errorUsers   = @()   # Fout bij verwijderen

try {
    $group = Get-MgGroup -Filter "displayName eq '$GroupName'" -ErrorAction Stop
    if ($null -eq $group) {
        Write-Log "Groep '$GroupName' niet gevonden." "ERROR"
        Send-SlackMessage ":x: *m365-offboarding-cleanup* — Groep '$GroupName' niet gevonden in Entra ID."
        exit 1
    }

    $allMembers = Get-MgGroupMember -GroupId $group.Id -All |
        ForEach-Object {
            Get-MgUser -UserId $_.Id -Property "Id,DisplayName,UserPrincipalName,OnPremisesSyncEnabled"
        }
} catch {
    Write-Log "Fout bij ophalen groepsleden: $_" "ERROR"
    Send-SlackMessage ":x: *m365-offboarding-cleanup* — Fout bij ophalen groepsleden: $_"
    exit 1
}

Write-Log "$($allMembers.Count) gebruikers gevonden in '$GroupName'."

foreach ($user in $allMembers) {
    # Hybrid/synced overslaan
    if ($user.OnPremisesSyncEnabled -eq $true) {
        Write-Log "SKIP (HYBRID): $($user.UserPrincipalName)"
        $skippedSync += $user.UserPrincipalName
        continue
    }

    try {
        $signInActivity = (Get-MgUser -UserId $user.Id -Property "SignInActivity").SignInActivity
        $lastActivity   = $signInActivity.LastSignInDateTime

        # Bewust enkel interactieve logins — non-interactive (bv. PowerBI service calls)
        # reflecteren geen echte gebruikersactiviteit en worden genegeerd.
        if ($null -eq $lastActivity) {
            $lastActivity = (Get-MgUser -UserId $user.Id -Property "CreatedDateTime").CreatedDateTime
        }

        $daysInactive = [math]::Floor(($today - $lastActivity).TotalDays)
        $daysLeft     = $DaysBeforeDelete - $daysInactive

        if ($lastActivity -lt $cutoffDate) {
            Write-Log "IN AANMERKING: $($user.UserPrincipalName) | Laatste activiteit: $lastActivity ($daysInactive dagen geleden)"
            $toDelete += [PSCustomObject]@{
                DisplayName   = $user.DisplayName
                UPN           = $user.UserPrincipalName
                Id            = $user.Id
                LastActivity  = $lastActivity
                DaysInactive  = $daysInactive
            }
        } else {
            Write-Log "NOG NIET: $($user.UserPrincipalName) | Laatste activiteit: $lastActivity (nog $daysLeft dagen)"
            $notYet += [PSCustomObject]@{
                UPN          = $user.UserPrincipalName
                LastActivity = $lastActivity.ToString("yyyy-MM-dd")
                DaysLeft     = $daysLeft
            }
        }
    } catch {
        Write-Log "Kon activiteit niet ophalen voor $($user.UserPrincipalName): $_" "WARN"
        $warnUsers += $user.UserPrincipalName
    }
}

Write-Log "$($toDelete.Count) gebruikers komen in aanmerking voor verwijdering."
#endregion

#region DELETE OR DRY RUN
if ($toDelete.Count -eq 0) {
    Write-Log "Geen accounts te verwijderen."
} elseif ($DryRun) {
    Write-Log "=== DRY RUN — Geen accounts worden verwijderd ===" "WARN"
    foreach ($user in $toDelete) {
        Write-Log "  [DRY RUN] Zou verwijderen: $($user.DisplayName) ($($user.UPN)) | Laatste activiteit: $($user.LastActivity)"
    }
} else {
    Write-Log "=== LIVE RUN — Accounts worden permanent verwijderd ===" "WARN"
    foreach ($user in $toDelete) {
        try {
            Remove-MgUser -UserId $user.Id -ErrorAction Stop
            Write-Log "VERWIJDERD: $($user.DisplayName) ($($user.UPN))"
            $deletedUsers += $user.UPN
        } catch {
            Write-Log "FOUT bij verwijderen van $($user.UPN): $_" "ERROR"
            $errorUsers += $user
        }
    }
    Write-Log "Verwijdering voltooid."
}
#endregion

#region SLACK NOTIFICATION
$runMode = if ($DryRun) { "DRY RUN" } else { "LIVE RUN" }
$emoji   = if ($DryRun) { ":test_tube:" } else { ":wastebasket:" }

$lines = @(
    "$emoji *m365-offboarding-cleanup* — $startTime  |  $runMode",
    "*Gevonden in groep:* $($allMembers.Count)"
    ""
)

# Verwijderd of zou verwijderd worden
if ($DryRun -and $toDelete.Count -gt 0) {
    $lines += ":wastebasket: *Zou verwijderen ($($toDelete.Count)):*"
    $toDelete | ForEach-Object { $lines += "  • $($_.UPN)  _(laatste activiteit: $($_.LastActivity.ToString('yyyy-MM-dd')), $($_.DaysInactive) dagen geleden)_" }
    $lines += ""
} elseif (-not $DryRun -and $deletedUsers.Count -gt 0) {
    $lines += ":white_check_mark: *Verwijderd ($($deletedUsers.Count)):*"
    $deletedUsers | ForEach-Object { $lines += "  • $_" }
    $lines += ""
} elseif ($toDelete.Count -eq 0) {
    $lines += ":white_check_mark: *Verwijderd (0)*"
    $lines += ""
}

# Nog niet in aanmerking
if ($notYet.Count -gt 0) {
    $lines += ":hourglass_flowing_sand: *Nog niet in aanmerking ($($notYet.Count)):*"
    $notYet | Sort-Object DaysLeft | ForEach-Object {
        $lines += "  • $($_.UPN)  _(laatste activiteit: $($_.LastActivity), nog $($_.DaysLeft) dagen)_"
    }
    $lines += ""
}

# Hybrid/synced geskipt
if ($skippedSync.Count -gt 0) {
    $lines += ":arrows_counterclockwise: *Geskipt — hybrid/synced ($($skippedSync.Count)):*"
    $skippedSync | ForEach-Object { $lines += "  • $_" }
    $lines += ""
}

# Fouten bij verwijderen
if ($errorUsers.Count -gt 0) {
    $lines += ":x: *Fout bij verwijderen ($($errorUsers.Count)):*"
    $errorUsers | ForEach-Object { $lines += "  • $($_.UPN)" }
    $lines += ""
}

# Activiteit niet opgehaald
if ($warnUsers.Count -gt 0) {
    $lines += ":warning: *Activiteit niet opgehaald ($($warnUsers.Count)):*"
    $warnUsers | ForEach-Object { $lines += "  • $_" }
    $lines += ""
}

Send-SlackMessage ($lines -join "`n")
#endregion

#region DISCONNECT
Disconnect-MgGraph | Out-Null
Write-Log "Verbinding verbroken. Log: $LogFile"
#endregion