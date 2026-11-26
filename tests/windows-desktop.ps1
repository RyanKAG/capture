# Tests the single-file download in an interactive Windows session. No mail account required.
param([string] $FixtureTitle)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

if ($FixtureTitle) {
    $form = New-Object Windows.Forms.Form
    $form.Text = $FixtureTitle
    $form.StartPosition = 'CenterScreen'
    $form.ClientSize = New-Object Drawing.Size(380, 160)
    $form.BackColor = [Drawing.Color]::FromArgb(27, 188, 210)
    $right = New-Object Windows.Forms.Panel
    $right.Width = 190
    $right.Dock = 'Right'
    $right.BackColor = [Drawing.Color]::FromArgb(245, 189, 38)
    $form.Controls.Add($right)
    [Windows.Forms.Application]::Run($form)
    return
}

$source = Join-Path $PSScriptRoot '../downloads/CaptureMail.exe'
$isolated = Join-Path ([IO.Path]::GetTempPath()) ('CaptureMail-test-' + [guid]::NewGuid())
$null = New-Item -ItemType Directory -Path $isolated
$exe = Join-Path $isolated 'CaptureMail.exe'
Copy-Item $source $exe
$app = $null
$fixture = $null
try {
    $app = Start-Process $exe -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 200
        $app.Refresh()
    } while (!$app.HasExited -and $app.MainWindowHandle -eq [IntPtr]::Zero -and [DateTime]::UtcNow -lt $deadline)
    if ($app.HasExited -or $app.MainWindowHandle -eq [IntPtr]::Zero) { throw 'The standalone EXE did not open its main window.' }
    Write-Output "Standalone app opened: $($app.MainWindowTitle)"
    $null = $app.CloseMainWindow()
    if (!$app.WaitForExit(10000)) { throw 'The idle app did not exit when its window closed.' }

    $title = 'CaptureMail QA ' + [guid]::NewGuid().ToString('N')
    $fixture = Start-Process powershell.exe -ArgumentList @('-NoProfile', '-STA', '-File', ('"' + $PSCommandPath + '"'), '-FixtureTitle', ('"' + $title + '"')) -PassThru
    $assembly = [Reflection.Assembly]::LoadFrom($exe)
    $api = $assembly.GetType('CaptureMail.WindowCapture', $true)
    $flags = [Reflection.BindingFlags]'Static,Public,NonPublic'
    $list = $api.GetMethod('List', $flags)
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 250
        $target = $list.Invoke($null, @()) | Where-Object { $_.ToString() -eq $title } | Select-Object -First 1
    } while (!$target -and !$fixture.HasExited -and [DateTime]::UtcNow -lt $deadline)
    if (!$target) { throw 'The window picker did not list the QA window.' }
    $capture = $api.GetMethods($flags) | Where-Object { $_.Name -eq 'CaptureAsync' -and $_.GetParameters().Count -eq 1 }
    $task = $capture.Invoke($null, @($target.PSObject.BaseObject))
    if (!$task.Wait(30000)) { throw 'Capturing the fixture timed out.' }
    $stream = [IO.MemoryStream]::new([byte[]]$task.Result)
    $png = [Drawing.Bitmap]::new($stream)
    try {
        $left = $png.GetPixel([int]($png.Width / 4), [int]($png.Height * 0.7))
        $right = $png.GetPixel([int]($png.Width * 0.75), [int]($png.Height * 0.7))
        if ($left.G -lt 145 -or $left.B -lt 150 -or $left.R -gt 80 -or
            $right.R -lt 180 -or $right.G -lt 140 -or $right.B -gt 100) {
            throw "The PNG did not contain both fixture markers. Got $left and $right."
        }
        Write-Output "Window capture passed: $($png.Width) x $($png.Height) PNG with both fixture markers."
    } finally { $png.Dispose(); $stream.Dispose() }
    $mail = $assembly.GetType('CaptureMail.GraphMail', $true)
    $check = $mail.GetMethod('CheckSdkAsync', $flags).Invoke($null, @())
    if (!$check.Wait(240000)) { throw 'Microsoft Graph SDK preparation timed out.' }
    Write-Output "Microsoft Graph authentication module imported: $($check.Result)"
} catch {
    $detail = $_.Exception.GetBaseException().Message
    Write-Output ("::error::" + $detail.Replace('%','%25').Replace("`r",'%0D').Replace("`n",'%0A'))
    throw
} finally {
    foreach ($process in @($fixture, $app)) {
        if ($process) {
            if (!$process.HasExited) { $process.Kill(); $process.WaitForExit() }
            $process.Dispose()
        }
    }
    # Windows holds a loaded assembly open until this test process exits.
    Remove-Item $isolated -Recurse -Force -ErrorAction SilentlyContinue
}
