# Run and verify the Windows screenshot emailer with classic Outlook

The script takes a PNG of one window and sends it through the account you choose in **Outlook (classic)**. A Hotmail account can send to a Gmail address. Outlook handles the sender login. The recipient needs no setup on the Windows PC. This method uses the Outlook Object Model, which Microsoft supports in classic Outlook for Windows. [Microsoft's Outlook feature comparison](https://support.microsoft.com/en-us/outlook/getstarted/feature-comparison-between-new-outlook-and-classic-outlook)

## 1. Prepare the PC

1. Open **Start → Outlook (classic)**. Make sure you can send a normal email from the account you want the script to use. If Office shows an activation or expired-trial prompt, resolve it before testing automation.
2. Install **Python 3.13 for Windows, 64-bit (x64)**, with the `py` launcher. [Python downloads for Windows](https://www.python.org/downloads/windows/)
3. Download the Windows setup ZIP and choose **Extract All**. Open the extracted folder containing `capture_email.py`. In File Explorer's address bar, type `powershell` and press **Enter** to open a terminal in that folder. [Download the Windows setup ZIP](https://raw.githubusercontent.com/RyanKAG/capture/main/downloads/windows-outlook-setup.zip)
4. Run these commands once. The first should print `win-amd64`. Wait for the dependency installation to finish.

```powershell
py -3.13 -c "import sysconfig; print(sysconfig.get_platform())"
py -3.13 -m pip install -r requirements.txt
```

## 2. Choose the window, sender and recipient

Open the target window and leave it unminimized. For a first test, you can open a saved Notepad document containing a phrase you will recognize in the screenshot. List the window titles with this command.

```powershell
py -3.13 capture_email.py --list-windows
```

Open `config.ini` in Notepad. The Windows setup ZIP already contains this file. If you downloaded the repository source instead, first run `Copy-Item config.example.ini config.ini`.

Set `title` to one complete title from the list. Set `sender` to the email address of your sending account in classic Outlook and `recipients` to the destination address. Keep `transport = mail_app`. Replace the example values below with yours and save the file.

```ini
[window]
title = Screenshot check.txt - Notepad

[email]
transport = mail_app
sender = sender@example.com
recipients = recipient@example.com

[schedule]
mode = interval
interval_minutes = 30
daily_times = 09:00, 17:30
```

## 3. Verify the screenshot locally

Run these lines together. They save a uniquely named preview and open it after a successful capture.

```powershell
$preview = "preview-$(Get-Date -Format yyyyMMdd-HHmmss-fff).png"
py -3.13 capture_email.py --preview $preview
if ($LASTEXITCODE -eq 0) { Invoke-Item $preview }
```

The preview passes if the PNG shows only the window you chose, with its content readable. If the script reports a missing, minimized or duplicate window, restore it, run `--list-windows` again and update `title` to a unique entry.

## 4. Verify one real email

```powershell
py -3.13 capture_email.py --once
```

1. Look for `Submitted the screenshot` in PowerShell. This means Outlook accepted the send request.
2. Open **Sent Items** in the sending Outlook account. Find the new message with a subject beginning **Window screenshot**, check the recipient and confirm it has one PNG attachment.
3. Open the message in the **recipient's inbox** and open the PNG. Delivery is verified when the message is from the configured sender and its attachment shows the intended window.

If the message is missing, check Outlook's **Outbox** for a queued message and the recipient's **Spam/Junk** folder before repeating `--once`. If the script reports that it found zero matching accounts, check the address in `config.ini` against the account in **classic Outlook**. If Outlook asks for send approval every time, the scheduled job will also require that approval.

## 5. Start and verify the schedule

For a quick test, set `mode = interval` and `interval_minutes = 1` in `config.ini`, save it, and start the schedule with this command.

```powershell
py -3.13 capture_email.py
```

Interval mode sends immediately on startup, then repeats. After the first submission, change the text in your test document and save it so its window title stays the same for the next capture. After the second scheduled submission, press **Ctrl+C** in PowerShell. Open both messages in the recipient's inbox and verify that the second attachment shows the updated content.

Set your regular schedule and restart the script. For a half-hour interval use `interval_minutes = 30`. For fixed times, use `mode = daily` and set `daily_times = 09:00, 17:30` on the PC's local clock. The setup ZIP also includes **Start screenshot schedule.cmd**, which you can double-click for regular use.

Keep one copy of the script running in your signed-in, unlocked Windows session. Keep the PC awake and the target window open and unminimized. Press **Ctrl+C** in the console to stop.
