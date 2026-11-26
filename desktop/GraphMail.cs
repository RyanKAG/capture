using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net.Http;
using System.Net.Mail;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace CaptureMail
{
    internal static class GraphMail
    {
        private const string Version = "2.41.2";
        private const int PackageSize = 7668657;
        // SHA-512 published with this exact Microsoft.Graph.Authentication package on the PowerShell Gallery.
        private const string PackageHash = "G9Vlb1p5IGamsIwbLNV8fbiL5QsVaViVk45wztFMzpoLzb4+7j3giJXsBbm/B5sYeJrf53ZiDurcJk6Ns8kIJQ==";
        private static readonly SemaphoreSlim Gate = new SemaphoreSlim(1);
        private static Process worker;
        private static string moduleDirectory;

        // Only this fixed program is interpreted as PowerShell. Mail data travels through
        // the process's redirected input. The SDK owns OAuth and token refresh.
        private const string WorkerScript = @"
$ErrorActionPreference = 'Stop'
$WarningPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
function Reply($kind, [string]$text) {
    $data = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text))
    [Console]::Out.WriteLine('CAPTUREMAIL ' + $kind + ' ' + $data)
    [Console]::Out.Flush()
}
function Read-Identity {
    $user = Invoke-MgGraphRequest -Method GET -Uri 'https://graph.microsoft.com/v1.0/me?$select=id,mail,userPrincipalName'
    $address = $user.mail
    if (!$address) { $address = $user.userPrincipalName }
    if (!$user.id -or !$address) { throw 'Microsoft did not return a mailbox address for this account.' }
    return @{id = $user.id; address = $address}
}
try {
    $configured = @(Get-ExecutionPolicy -List | Where-Object { $_.ExecutionPolicy -ne 'Undefined' })
    if ($configured.Count -eq 0) { Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force }
    Import-Module -Name $env:CAPTUREMAIL_SDK -ErrorAction Stop
    if ($env:CAPTUREMAIL_ACTION -eq 'check') {
        $mod = Get-Module Microsoft.Graph.Authentication
        $null = Get-Command Connect-MgGraph, Invoke-MgGraphRequest, Set-MgRequestContext -ErrorAction Stop
        Reply 'OK' $mod.Version.ToString()
        exit 0
    }
    Connect-MgGraph -TenantId consumers -Scopes 'User.Read','Mail.Send' -ContextScope CurrentUser -NoWelcome -ErrorAction Stop
    Set-MgRequestContext -MaxRetry 0 -ClientTimeout 45 | Out-Null
    $original = Read-Identity
    Reply 'OK' $original.address
    while ($null -ne ($senderLine = [Console]::In.ReadLine())) {
        $payload = [Console]::In.ReadLine()
        if ($null -eq $payload) { break }
        $expected = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($senderLine))
        $current = Read-Identity
        if ($current.id -ne $original.id -or $current.address -ne $expected) {
            throw 'The signed-in account changed. Sign in again and confirm the sending address.'
        }
        $body = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($payload))
        $null = Invoke-MgGraphRequest -Method POST -Uri 'https://graph.microsoft.com/v1.0/me/sendMail' -ContentType 'application/json; charset=utf-8' -Body $body -StatusCodeVariable code
        if ($code -ne 202) { throw 'Unexpected Microsoft response. Check Sent Items before sending again.' }
        Reply 'OK' 'Accepted'
        $body = $null
        $payload = $null
    }
} catch {
    Reply 'ERROR' $_.Exception.Message
    exit 1
}";

        static GraphMail()
        {
            AppDomain.CurrentDomain.ProcessExit += (s, e) => {
                StopWorker();
                if (moduleDirectory != null) {
                    try { Directory.Delete(moduleDirectory, true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
                }
            };
        }

        internal static Task<string> ConnectAsync() { return StartAsync(false); }
        // Used by the Windows smoke test to exercise the real download and import without logging in or sending.
        internal static Task<string> CheckSdkAsync() { return StartAsync(true); }

        private static async Task<string> StartAsync(bool checkOnly)
        {
            await Gate.WaitAsync().ConfigureAwait(false);
            try {
                StopWorker();
                string module = await PrepareModuleAsync().ConfigureAwait(false);
                var start = new ProcessStartInfo {
                    FileName = Path.Combine(Environment.SystemDirectory, @"WindowsPowerShell\v1.0\powershell.exe"),
                    Arguments = "-NoLogo -NoProfile -NonInteractive -EncodedCommand " +
                        Convert.ToBase64String(Encoding.Unicode.GetBytes(WorkerScript)),
                    UseShellExecute = false, RedirectStandardInput = true,
                    RedirectStandardOutput = true, RedirectStandardError = true,
                    // WAM needs a valid parent console HWND even when the console is hidden.
                    CreateNoWindow = false, WindowStyle = ProcessWindowStyle.Hidden
                };
                start.EnvironmentVariables["CAPTUREMAIL_SDK"] = module;
                start.EnvironmentVariables["CAPTUREMAIL_ACTION"] = checkOnly ? "check" : "connect";
                var process = Process.Start(start);
                worker = process;
                process.BeginErrorReadLine(); // drain diagnostics without logging addresses or attachment data
                string answer = await WithTimeout(ReadReplyAsync(process), checkOnly ? 60 : 300).ConfigureAwait(false);
                if (checkOnly) StopWorker();
                return answer;
            } catch {
                StopWorker();
                throw;
            } finally { Gate.Release(); }
        }

        internal static async Task SendAsync(string expectedSender, string recipient, string title, byte[] png)
        {
            if (png == null || png.Length == 0) throw new ArgumentException("The screenshot was empty.");
            if (png.Length > 2900000) throw new ArgumentException("This screenshot is too large for a single Graph send. Make the target window smaller and try again.");
            if (new MailAddress(recipient).Address != recipient) throw new ArgumentException("Enter one recipient email address.");
            var now = DateTimeOffset.Now;
            var attachment = new Dictionary<string, object> {
                {"@odata.type", "#microsoft.graph.fileAttachment"}, {"name", "screenshot-" + now.ToString("yyyyMMdd-HHmmss") + ".png"},
                {"contentType", "image/png"}, {"contentBytes", Convert.ToBase64String(png)}
            };
            var mail = new {
                message = new {
                    subject = "Window screenshot — " + now.ToString("yyyy-MM-dd HH:mm zzz"),
                    body = new { contentType = "Text", content = "Window: " + title + "\r\nCaptured: " + now.ToString("O") },
                    toRecipients = new[] { new { emailAddress = new { address = recipient } } },
                    attachments = new[] { attachment }
                }, saveToSentItems = true
            };
            var serializer = new JavaScriptSerializer { MaxJsonLength = 5 * 1024 * 1024 };
            string body = Convert.ToBase64String(Encoding.UTF8.GetBytes(serializer.Serialize(mail)));
            await Gate.WaitAsync().ConfigureAwait(false);
            try {
                var process = worker;
                if (process == null || process.HasExited) throw new InvalidOperationException("Sign in to your Microsoft account again.");
                string sender = Convert.ToBase64String(Encoding.UTF8.GetBytes(expectedSender));
                string reply = await WithTimeout(SendRequestAsync(process, sender, body), 110).ConfigureAwait(false);
                if (reply != "Accepted") throw new IOException("The email submission could not be confirmed.");
            } catch (Exception ex) {
                StopWorker();
                throw new InvalidOperationException(ex.Message + " Check Sent Items before retrying, then sign in again.", ex);
            } finally { Gate.Release(); }
        }

        private static async Task<string> SendRequestAsync(Process process, string sender, string body)
        {
            await process.StandardInput.WriteLineAsync(sender).ConfigureAwait(false);
            await process.StandardInput.WriteLineAsync(body).ConfigureAwait(false);
            await process.StandardInput.FlushAsync().ConfigureAwait(false);
            return await ReadReplyAsync(process).ConfigureAwait(false);
        }

        private static async Task<string> ReadReplyAsync(Process process)
        {
            string line;
            while ((line = await process.StandardOutput.ReadLineAsync().ConfigureAwait(false)) != null) {
                if (!line.StartsWith("CAPTUREMAIL ", StringComparison.Ordinal)) continue;
                var parts = line.Split(new[] {' '}, 3);
                if (parts.Length != 3) break;
                string value = Encoding.UTF8.GetString(Convert.FromBase64String(parts[2]));
                if (parts[1] == "OK") return value;
                if (parts[1] == "ERROR") throw new InvalidOperationException(value.Length > 550 ? value.Substring(0, 550) : value);
            }
            throw new IOException("The Microsoft mail helper closed unexpectedly. Open the app again and check your network connection.");
        }

        private static async Task<string> WithTimeout(Task<string> operation, int seconds)
        {
            using (var cancel = new CancellationTokenSource()) {
                if (await Task.WhenAny(operation, Task.Delay(TimeSpan.FromSeconds(seconds), cancel.Token)).ConfigureAwait(false) != operation) {
                    throw new TimeoutException("Microsoft did not respond in time.");
                }
                cancel.Cancel();
                return await operation.ConfigureAwait(false);
            }
        }

        private static void StopWorker()
        {
            var process = Interlocked.Exchange(ref worker, null);
            if (process == null) return;
            try {
                if (!process.HasExited) {
                    process.Kill();
                    process.WaitForExit(3000);
                }
            } catch (InvalidOperationException) { }
              catch (System.ComponentModel.Win32Exception) { }
            finally { process.Dispose(); }
        }

        private static bool IsExpectedPackage(string archive)
        {
            if (!File.Exists(archive) || new FileInfo(archive).Length != PackageSize) return false;
            using (var stream = File.OpenRead(archive))
            using (var hash = SHA512.Create())
                return Convert.ToBase64String(hash.ComputeHash(stream)) == PackageHash;
        }

        private static async Task<string> PrepareModuleAsync()
        {
            const string manifest = "Microsoft.Graph.Authentication.psd1";
            if (moduleDirectory != null) return Path.Combine(moduleDirectory, manifest);
            string cache = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CaptureMail", "MicrosoftGraph");
            Directory.CreateDirectory(cache);
            string archive = Path.Combine(cache, "Microsoft.Graph.Authentication." + Version + ".nupkg");
            if (!IsExpectedPackage(archive)) {
                string temp = archive + "." + Guid.NewGuid().ToString("N") + ".tmp";
                try {
                    using (var http = new HttpClient { Timeout = TimeSpan.FromMinutes(3) }) {
                        var bytes = await http.GetByteArrayAsync("https://cdn.powershellgallery.com/packages/microsoft.graph.authentication." + Version + ".nupkg").ConfigureAwait(false);
                        if (bytes.Length != PackageSize) throw new InvalidDataException("The Microsoft Graph download was incomplete.");
                        File.WriteAllBytes(temp, bytes);
                    }
                    if (!IsExpectedPackage(temp)) throw new InvalidDataException("The Microsoft Graph package did not match the verified release.");
                    if (File.Exists(archive)) File.Replace(temp, archive, null);
                    else File.Move(temp, archive);
                } finally { File.Delete(temp); }
            }
            // Extract a fresh, verified copy for this process. Only the original package is cached across runs.
            string folder = Path.Combine(Path.GetTempPath(), "CaptureMail-SDK-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(folder);
            try {
                using (var zip = ZipFile.OpenRead(archive)) {
                    foreach (var entry in zip.Entries) {
                        string dest = Path.GetFullPath(Path.Combine(folder, entry.FullName));
                        if (!dest.StartsWith(folder + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                            throw new InvalidDataException("Invalid path in the SDK package.");
                        if (entry.Name.Length == 0) continue;
                        Directory.CreateDirectory(Path.GetDirectoryName(dest));
                        entry.ExtractToFile(dest);
                    }
                }
                moduleDirectory = folder;
                return Path.Combine(folder, manifest);
            } catch { Directory.Delete(folder, true); throw; }
        }
    }
}
