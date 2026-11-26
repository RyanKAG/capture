# Email window screenshots on macOS and Windows

Email a PNG of one application window at a regular interval or at chosen times of day. Run the script in the desktop session where the window is open.

**Windows with classic Outlook:** [Download the Windows setup ZIP](downloads/windows-outlook-setup.zip). Extract it and follow the included README to set your window title, sending account, recipient, and schedule.

## Test the schedule on this Mac

Double-click `Test on Mac.command` in Finder. Choose a window number and press Return at the email address prompt. The script captures the window twice, 15 seconds apart, then stops. It prints the path to a new `screenshot-test-…` folder. Open either `.eml` file to see its timestamp and PNG attachment. This local test works without a mail account.

To test actual inbox delivery, enter an address already connected to Apple Mail at the same prompt. The script sends both screenshots to that address using Mail. To connect Gmail, open **Mail → Add Account** and follow Google's sign-in. [Apple Mail account setup](https://support.apple.com/guide/mail/mail35803/mac)

The Terminal equivalent is `./.venv/bin/python capture_email.py --test-schedule` from the project folder. Both tests use the same capture and scheduling code as ongoing runs. Classic Outlook delivery uses the Windows app and requires a separate Windows test.

## Try one screenshot

For a quick manual check, attach a local preview PNG in your usual Gmail browser tab and send it to yourself. The scheduled script can send through a desktop mail app using that app's Google sign-in.

On macOS, open **Mail → Add Account → Google** and sign in with your usual Google account if it is not already in Mail. Open the target window. The following command lists the windows, asks for a window number and the email address configured in Mail, then submits one screenshot to that same address. macOS may ask you to allow the terminal to control Mail. [Google account setup for mail apps](https://support.google.com/mail/answer/7126229)

On this Mac, open Terminal in the project folder and run

```sh
./.venv/bin/python capture_email.py --mail-app-test
```

On a fresh Mac installation, first create the virtual environment from the extracted project folder using Python 3.13.

```sh
python3.13 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

If macOS asks, enable Terminal under **System Settings → Privacy & Security → Screen & System Audio Recording**, then reopen Terminal and rerun the command. Keep the target window open on the current desktop.

## Set up on Windows

Use x64 Python on Windows 11 or Windows 10 version 1903 or later. The commands below assume the Windows Python launcher (`py`) selects Python 3.12 or 3.13. [Windows capture API requirements](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow) · [Capture package for x64 Python](https://pypi.org/project/windows-capture/2.0.1/)

Open **Outlook (classic)** and check that you can send from the account you plan to use. A Hotmail or Outlook.com account can send to a Gmail recipient. Open PowerShell in the folder containing `capture_email.py` and run the commands below for a one-shot test to the sending account's own inbox. To test a different recipient, follow the configuration steps below and use `--once`. Outlook may show a programmatic access prompt depending on its settings. [Compare Outlook versions](https://support.microsoft.com/en-us/outlook/getstarted/feature-comparison-between-new-outlook-and-classic-outlook)

```powershell
py -m pip install -r requirements.txt
py capture_email.py --mail-app-test
```

### Automate Outlook on Windows

1. Open the window you want to capture. In the project folder, run `py capture_email.py --list-windows` to find its exact title.
2. Copy `config.example.ini` to `config.ini` if you are running from the source folder. The Windows setup ZIP already includes `config.ini`. Set `title`, `sender` (the address signed in to classic Outlook), and `recipients`. Keep `transport = mail_app`. Use `mode = interval` and `interval_minutes = 30` for a capture now and every half hour, or use `mode = daily` and `daily_times = 09:00, 17:30` for fixed local times.
3. Run `py capture_email.py --once` and check that the intended inbox receives the PNG. If Outlook asks you to approve programmatic sending each time, your Outlook settings or organization policy require interaction for this method.
4. Double-click `Start screenshot schedule.cmd` to run the schedule. Keep the Windows session unlocked, with the target window open and unminimized. Press Ctrl+C in the console to stop.

To restart the schedule automatically when you sign in, place a **shortcut** to `Start screenshot schedule.cmd` in your Windows Startup folder. Open that folder with **Win+R**, then enter `shell:startup`. Run one copy of the scheduler to avoid duplicate emails. Removing the shortcut turns off automatic startup. [Microsoft's Startup folder instructions](https://support.microsoft.com/en-us/windows/experience/startup-boot/configure-startup-applications-in-windows)

## Configure scheduled captures

Copy `config.example.ini` to `config.ini` if you have not already done so. On macOS, run `./.venv/bin/python capture_email.py --list-windows`. On Windows, run `py capture_email.py --list-windows`. In `[window]`, paste a complete title unique to the window you want. Matching ignores capitalization. If the title changes, update the file and restart the script.

The example uses `transport = mail_app`. Set `sender` to your address in Apple Mail or classic Outlook, and `recipients` to the address that should receive the screenshots. Separate multiple recipients with commas. Recipients can use any email service.

### Direct SMTP delivery

For a direct Gmail SMTP connection, set `transport = smtp`, `host = smtp.gmail.com`, `port = 587`, `security = starttls`, and `username` to the full Gmail address. The script prompts for your Google app password. You can test this mode with `--gmail-test`. For unattended SMTP, supply the credential in `SCREENSHOT_SMTP_PASSWORD`. For a different sending provider, use its host, port and TLS settings. [Gmail SMTP settings](https://developers.google.com/workspace/gmail/imap/imap-smtp) · [Google app passwords](https://support.google.com/accounts/answer/185833)

For an SMTP account that requires OAuth, set `username` and add `oauth_token_file` under `[email]` with the full path to a file containing a current XOAUTH2 access token. Your token provider must refresh that file before expiry. The script reads it on each send. For a trusted TLS relay that authenticates your computer another way, leave `username` empty.

## Choose a schedule

To send immediately and then every 30 minutes, set

```ini
[schedule]
mode = interval
interval_minutes = 30
```

Fractional minutes work too. For example, `0.5` means 30 seconds and `120` means two hours. To send twice a day at specific times on the computer's local clock, use

```ini
[schedule]
mode = daily
daily_times = 09:00, 17:30
```

## Preview, send, and run

On macOS, save a local preview to a new filename and open it with

```sh
./.venv/bin/python capture_email.py --preview preview.png
open preview.png
```

Run `./.venv/bin/python capture_email.py --once` to send a single email to the recipients in your configuration. Run `./.venv/bin/python capture_email.py` to start the schedule.

On Windows, use the following commands for the preview, single email and schedule.

```powershell
py capture_email.py --preview preview.png
Invoke-Item preview.png
```

To send one test email and exit, run

```powershell
py capture_email.py --once
```

To start the configured schedule, run

```powershell
py capture_email.py
```

Keep the terminal running in your unlocked desktop session. Press Ctrl+C to stop. The target window can be covered by another window or maximized. Protected content can appear blank. On macOS each capture briefly uses a private temporary PNG that the script removes after reading. Scheduled runs log capture or delivery errors to the terminal. If the window is closed, minimized, or has the same title as another window, that attempt is skipped. The next scheduled attempt looks for the window again.

Daily times missed while the computer sleeps are skipped. An interval schedule resumes without a burst of catch-up emails.
