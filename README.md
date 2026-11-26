# captureandmail

A trusted person emails **status** to the server's Gmail account and gets a screenshot of the PC back as a reply.

```
trusted sender ──"status"──▶ Gmail inbox ◀──checks every 15s── server (Docker, :8989)
                                                                 │  "capture now" (only if sender is trusted)
                                                                 ▼
trusted sender ◀──"Re: status" + screenshot── server ◀──upload── client (Windows PC)
```

- The **server** checks the Gmail inbox (read-only) for new emails whose subject is exactly `status`
  and whose sender is on the **trusted senders** list. Emails from anyone else are ignored.
- The **client** keeps a connection open to the server and waits. On a request, it captures all screens
  and uploads the image. The server emails it **only to the sender who asked**, as a reply in the same thread.
- It always replies to the email's From address, never a Reply-To. Someone faking a trusted
  address can't receive the screenshot; it goes to the real owner of that address.
- `/send` only emails in answer to a pending request (valid for 1 hour), so an upload alone can't send anything.
- Screenshots are held in memory only. Neither the client nor the server writes them to disk.
- Every client connected to the server sends its own screenshot.

## Server (Docker Compose, port 8989)
1. Gmail: turn on 2-Step Verification, then create an App Password at
   https://myaccount.google.com/apppasswords. The same password is used to read the inbox and to send.
2. Run:

       cp config.env.example .env      # set ADMIN_PASSWORD; the rest can be done in the web page
       docker compose up -d --build

3. Open **http://<server-ip>:8989/** and enter the Gmail address, App Password and **trusted senders**.
   Click **Save settings**, then **Test Gmail login**, **Send test email** and **Take screenshot now**.

Settings saved on the page are applied immediately, and they're kept in the Docker volume `settings`
(`/data/settings.json`). They override `.env`. The App Password is never sent back to the browser.
`docker compose logs -f` shows what the server is doing.

## Client (Windows)
    pip install -r client\requirements.txt
    python client\client.py --server http://<server-ip>:8989

To test without sending an email, pick a trusted address on the settings page and click **Take screenshot now**.

To start it automatically at login, use Task Scheduler: set the trigger to "At log on" and the action to
`pythonw.exe C:\path\client\client.py --server http://...`. It must run in the logged-in user's session.
If it runs as a Windows service, the screenshots come out black.

A send counts as done only when the server returns 200 **and** the sha256 of what it emailed matches the
screenshot. Otherwise the client resends the same image (5 attempts). The default format is jpeg;
`--format png` gives sharper text but much larger files.

## API
| Endpoint | |
|---|---|
| `GET /wait?after=<seq>&boot=<id>&client=<name>` | long-poll held up to 50s; `{"capture": true, "requests": [ids]}` means take a screenshot now |
| `POST /send` | multipart: `file` = image, `requests` = ids from `/wait`, `client` = name. Emails each requester; returns `{"ok": true, "sha256": ...}` |
| `GET /` | settings page (HTTP password = `ADMIN_PASSWORD`, any username) |
| `POST /trigger` | `{"to": "<trusted address>"}`: request a capture without sending an email |
| `GET /health` | `{"ok": true}` |

## Tests (no email account needed)
    python3 -m unittest -v
The tests cover:
- the inbox watcher: old emails ignored, exact subject match, allowed senders
- the trigger reaching waiting clients, including multiple clients
- a trigger that arrives between two client polls not being lost
- a server restart not leaving the client stuck
- retrying when sending fails
- the image arriving byte-for-byte, and nothing being written to disk
