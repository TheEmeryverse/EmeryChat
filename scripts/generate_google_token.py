import os
import json
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta, timezone

REPO_DIR = Path(__file__).resolve().parent.parent
GOOGLE_SECRETS_DIR = REPO_DIR / "secrets" / "google"
CALENDAR_CREDENTIALS_PATH = GOOGLE_SECRETS_DIR / "credentials.json"
NEST_CREDENTIALS_PATH = GOOGLE_SECRETS_DIR / "nest_credentials.json"


def _load_env_file(path):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _repo_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_DIR / path


def _load_nest_project_id():
    env_value = os.getenv("NEST_PROJECT_ID", "").strip()
    if env_value:
        return env_value

    integrations_path = REPO_DIR / "config" / "integrations.json"
    try:
        integrations = json.loads(integrations_path.read_text(encoding="utf-8"))
        return str(integrations.get("nest", {}).get("project_id", "")).strip()
    except (OSError, ValueError, TypeError):
        return ""


def _load_client_credentials(creds_file):
    with open(creds_file, "r", encoding="utf-8") as file_handle:
        creds_data = json.load(file_handle)

    client_config = creds_data.get("installed") or creds_data.get("web")
    if not client_config:
        raise ValueError("Unsupported credentials format; expected 'installed' or 'web'.")
    return client_config["client_id"], client_config["client_secret"]


def _extract_authorization_code(raw_code):
    if "code=" not in raw_code:
        return urllib.parse.unquote(raw_code)
    parsed_url = urllib.parse.urlparse(raw_code)
    query_params = urllib.parse.parse_qs(parsed_url.query)
    return urllib.parse.unquote(query_params.get("code", [raw_code])[0])


def _exchange_authorization_code(client_id, client_secret, code, redirect_uri):
    token_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=urllib.parse.urlencode(token_data).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def _find_credentials_file(choice):
    # If choice is 1, search for credentials.json
    # If choice is 2, search for nest_credentials.json first, then fallback to credentials.json
    search_names = ["credentials.json"] if choice == "1" else ["nest_credentials.json", "credentials.json"]
    for name in search_names:
        # Check secrets/google/
        path_secrets = GOOGLE_SECRETS_DIR / name
        if path_secrets.exists():
            return path_secrets
        # Check repo root
        path_root = REPO_DIR / name
        if path_root.exists():
            return path_root
    return None


_load_env_file(REPO_DIR / ".env")
GOOGLE_TOKEN_PATH = _repo_path(os.getenv("GOOGLE_TOKEN_PATH", "secrets/google/token.json"))
NEST_TOKEN_PATH = _repo_path(os.getenv("NEST_TOKEN_PATH", "secrets/google/nest_token.json"))
NEST_PROJECT_ID = _load_nest_project_id()


def generate_calendar_token(creds_file):
    print("\n========================================================")
    print("      Google Calendar Token Generator (Desktop Flow)    ")
    print("========================================================\n")
    scopes = ['https://www.googleapis.com/auth/calendar.readonly']
    print(f"Loading credentials from: {creds_file}")

    try:
        client_id, client_secret = _load_client_credentials(creds_file)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"❌ Error loading Calendar credentials: {exc}")
        return

    redirect_uri = "http://localhost"
    auth_params = {
        "access_type": "offline",
        "client_id": client_id,
        "prompt": "consent",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(auth_params)

    print("\nStep 1: Copy and open this URL in your browser:")
    print(f"\n👉 {auth_url}\n")
    print("Step 2: Approve Calendar access. The final localhost page may fail to load.")
    print("Step 3: Copy the full URL from the browser address bar and paste it below.")
    raw_code = input("\nPaste the authorization code or full redirect URL here: ").strip()
    if not raw_code:
        print("❌ Error: Code cannot be empty.")
        return

    try:
        token_data = _exchange_authorization_code(
            client_id,
            client_secret,
            _extract_authorization_code(raw_code),
            redirect_uri,
        )
        expires_in = token_data.get("expires_in", 3600)
        expiry_time = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat().replace("+00:00", "Z")
        token_json = {
            "token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": scopes,
            "universe_domain": "googleapis.com",
            "account": "",
            "expiry": expiry_time,
        }

        Path(GOOGLE_TOKEN_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(GOOGLE_TOKEN_PATH, 'w', encoding="utf-8") as token_file:
            json.dump(token_json, token_file)
            
        print(f"\n✅ Successfully generated {GOOGLE_TOKEN_PATH}!")
        print("Your Google Calendar integration is now ready to use.")
        print("\nNote: If your Google Cloud app's Publishing Status is set to 'Testing',")
        print("this token will expire in 7 days and you will need to run this script again.")
        print("To fix this permanently, change the status to 'In production' in the OAuth consent screen.")
    except Exception as exc:
        print(f"\n❌ An error occurred during Calendar authentication: {exc}")
        if hasattr(exc, "read"):
            print("Response details:", exc.read().decode("utf-8"))

def generate_nest_token(creds_file):
    print("\n========================================================")
    print("      Google Nest Token Generator Helper (PCM Flow)      ")
    print("========================================================\n")
    
    with open(creds_file, 'r') as f:
        creds_data = json.load(f)
        
    if 'installed' in creds_data:
        client_id = creds_data['installed']['client_id']
        client_secret = creds_data['installed']['client_secret']
    elif 'web' in creds_data:
        client_id = creds_data['web']['client_id']
        client_secret = creds_data['web']['client_secret']
    else:
        print("❌ Error: Unsupported credentials format.")
        return
        
    nest_project_id = NEST_PROJECT_ID
    if not nest_project_id or nest_project_id.strip() == "" or "YOUR_" in nest_project_id:
        print("❌ Error: nest.project_id is not configured in config/integrations.json.")
        print("Please add your Nest Device Access Project ID (UUID) under nest.project_id.")
        return
        
    print("Step 1: In your Nest Device Access Console, ensure your project's Redirect URI is set to:")
    print("👉 http://localhost\n")
    
    scopes = ['https://www.googleapis.com/auth/sdm.service']
    scope_str = " ".join(scopes)
    
    # Construct Nest Partner Connections authorization link
    params = {
        'access_type': 'offline',
        'client_id': client_id,
        'prompt': 'consent',
        'redirect_uri': 'http://localhost',
        'response_type': 'code',
        'scope': scope_str,
        'state': 'emerychat_auth'
    }
    encoded_params = urllib.parse.urlencode(params)
    auth_url = f"https://nestservices.google.com/partnerconnections/{nest_project_id}/auth?{encoded_params}"
    
    print("Step 2: Copy and open this URL in your web browser:")
    print(f"\n👉 {auth_url}\n")
    
    print("Step 3: Follow the instructions in the browser:")
    print("- Log in with the Google account that owns your Nest devices.")
    print("- Select your home and check the boxes next to your thermostats.")
    print("- Click Allow.")
    print("- Note: When finished, your browser will redirect to a page (which might appear blank or show a connection error).")
    print("- **Look at the browser URL bar** and copy the long code parameter after '?code='.")
    print("  (It starts with '4/' and ends before any '&state=')")
    
    raw_code = input("\nPaste the copied authorization code or full redirect URL here: ").strip()
    if not raw_code:
        print("❌ Error: Code cannot be empty.")
        return
        
    code = raw_code
    if "code=" in raw_code:
        try:
            parsed_url = urllib.parse.urlparse(raw_code)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            if 'code' in query_params:
                code = query_params['code'][0]
        except Exception:
            pass

    code = urllib.parse.unquote(code)
    
    print("\nExchanging code for tokens...")
    token_url = "https://oauth2.googleapis.com/token"
    token_data = {
        'client_id': client_id,
        'client_secret': client_secret,
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': 'http://localhost'
    }
    encoded_data = urllib.parse.urlencode(token_data).encode('utf-8')
    
    req = urllib.request.Request(token_url, data=encoded_data, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            
            access_token = res_data.get('access_token')
            refresh_token = res_data.get('refresh_token')
            expires_in = res_data.get('expires_in', 3600)
            
            expiry_time = (datetime.utcnow() + timedelta(seconds=expires_in)).strftime('%Y-%m-%dT%H:%M:%SZ')
            
            token_json = {
                "token": access_token,
                "refresh_token": refresh_token,
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": client_id,
                "client_secret": client_secret,
                "scopes": scopes,
                "universe_domain": "googleapis.com",
                "account": "",
                "expiry": expiry_time
            }
            
            Path(NEST_TOKEN_PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(NEST_TOKEN_PATH, 'w') as token_file:
                json.dump(token_json, token_file)
                
            print(f"\n✅ Successfully generated {NEST_TOKEN_PATH}!")
            print("Your Google Nest Thermostat integration is now ready to use.")
            
    except Exception as e:
        print(f"\n❌ Failed to exchange code for token: {e}")
        if hasattr(e, 'read'):
            print("Response details:", e.read().decode('utf-8'))

def main():
    print("========================================================")
    print("              EmeryChat Google Token Setup              ")
    print("========================================================")
    print(f"1. Google Calendar (generates {GOOGLE_TOKEN_PATH})")
    print(f"2. Google Nest Thermostat (generates {NEST_TOKEN_PATH})")
    choice = input("Select an option (1 or 2): ").strip()
    
    if choice not in ("1", "2"):
        print("❌ Invalid selection. Exiting.")
        return
        
    creds_file = _find_credentials_file(choice)
    if not creds_file:
        expected = "nest_credentials.json or credentials.json" if choice == "2" else "credentials.json"
        print(f"❌ Error: Credentials file ({expected}) not found.")
        print("Please place your credentials JSON file in the repository root or under the secrets/google/ directory.")
        return
        
    if choice == "1":
        generate_calendar_token(str(creds_file))
    else:
        generate_nest_token(str(creds_file))

if __name__ == '__main__':
    main()
