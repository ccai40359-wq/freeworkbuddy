# WebBuddy server deployment

## 1. Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Create a data directory that is not inside the Git checkout:

```bash
sudo install -d -m 700 -o webbuddy -g webbuddy /var/lib/webbuddy
```

## 2. Environment

Set these values in your process manager or a protected environment file:

```bash
WEBBUDDY_ADMIN_USERNAME=admin@local.com
WEBBUDDY_ADMIN_PASSWORD=use-a-new-long-password
WEBBUDDY_DATA_DIR=/var/lib/webbuddy
WEBBUDDY_SECURE_COOKIE=true
WEBBUDDY_ACCOUNT_SYNC_INTERVAL=300
```

The password is hashed with PBKDF2 on first startup. The plaintext value is not
written to `settings.json`. Environment credentials initialize a new data
directory only; an existing administrator record is not overwritten.

The account sync worker runs immediately on startup and then every five
minutes. It checks the server-side daily check-in status before claiming and
refreshes each account's real credit balance. Run one application process per
data directory so only one scheduler owns the imported account files.

## 3. Run

Keep the application bound to localhost when using a reverse proxy:

```bash
python webgui.py --host 127.0.0.1 --port 8788 --secure-cookie
```

Proxy HTTPS traffic to `http://127.0.0.1:8788`. Forward the original host and
scheme headers. Do not expose port 8788 directly to the public internet.

For a temporary direct test without a reverse proxy:

```bash
WEBBUDDY_SECURE_COOKIE=false python webgui.py --host 0.0.0.0 --port 8788
```

Direct HTTP mode should only be used on a trusted network.

## 4. Import an account and connect a client

On Windows, sign in to WorkBuddy first. Its desktop credential is normally at:

```text
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
```

Open the WebUI, go to **账号管理**, select **上传 auth 文件**, and import that
file. Then go to **API Keys**, select **生成 Key**, bind the required accounts,
and save the generated key.

Use these values in an OpenAI-compatible client:

```text
Local Base URL:  http://127.0.0.1:8788/v1
API Key:         the key generated in the WebUI
Model:           auto (or another model listed by /v1/models)
```

Hermes, Trae, and WorkBuddy have been tested with the same OpenAI-compatible
settings: select an OpenAI-compatible provider, use the Base URL above, paste
the WebUI-generated API Key, and select the `auto` model. If a client asks for
the full chat endpoint instead of a Base URL, use
`http://127.0.0.1:8788/v1/chat/completions`.

For a remote deployment, replace the local Base URL with the public HTTPS URL
ending in `/v1`.

For a friend connecting to your deployed server, the Base URL is your public
HTTPS address with `/v1`, for example `https://api.example.com/v1`. Never send
`127.0.0.1` for remote access because it points to the friend's own computer.
Share only the public Base URL and a generated API Key. The
`workbuddy-desktop.info` file contains login tokens and must not be shared.
