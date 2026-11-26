# Offline stand-in for the Graph service boundary. This module makes no network calls.
function Connect-MgGraph {
    [CmdletBinding()]
    param($TenantId, [string[]]$Scopes, $ContextScope, [switch]$NoWelcome)
    if ($TenantId -ne 'consumers' -or ($Scopes -notcontains 'Mail.Send') -or ($Scopes -notcontains 'User.Read')) {
        throw 'Unexpected account type or permissions.'
    }
}

function Set-MgRequestContext {
    param([int]$MaxRetry, [int]$ClientTimeout)
    if ($MaxRetry -ne 0) { throw 'The test expects automatic send retries to be disabled.' }
}

function Invoke-MgGraphRequest {
    param($Method, $Uri, $ContentType, $Body, $StatusCodeVariable)
    if ($Method -eq 'GET' -and $Uri -eq 'https://graph.microsoft.com/v1.0/me?$select=id,mail,userPrincipalName') {
        return @{ id = 'qa-account'; mail = 'sender@example.invalid' }
    }
    if ($Method -ne 'POST' -or $Uri -ne 'https://graph.microsoft.com/v1.0/me/sendMail') { throw 'Unexpected Graph request.' }
    $envelope = ConvertFrom-Json $Body
    $message = $envelope.message
    if ($message.toRecipients.Count -ne 1 -or $message.toRecipients[0].emailAddress.address -ne 'recipient@example.invalid' -or
        $message.attachments.Count -ne 1 -or $message.subject -notlike 'Window screenshot*' -or !$envelope.saveToSentItems) {
        throw 'Unexpected email envelope.'
    }
    $attachment = $message.attachments[0]
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $digest = [Convert]::ToBase64String($hash.ComputeHash([Convert]::FromBase64String($attachment.contentBytes))) }
    finally { $hash.Dispose() }
    if ($digest -ne $env:CAPTUREMAIL_TEST_PNG -or $attachment.contentType -ne 'image/png' -or
        $attachment.'@odata.type' -ne '#microsoft.graph.fileAttachment' -or $attachment.name -notlike '*.png') {
        throw 'The message did not attach the captured PNG intact.'
    }
    Set-Variable -Name $StatusCodeVariable -Value 202 -Scope Global
}
