import os
import json
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta

from emery.config import GOOGLE_TOKEN_PATH, NEST_PROJECT_ID, NEST_TOKEN_PATH

REPO_DIR = Path(__file__).resolve().parent.parent
CALENDAR_CREDENTIALS_PATH = REPO_DIR / "secrets" / "credentials.json"
NEST_CREDENTIALS_PATH = REPO_DIR / "secrets" / "nest_credentials.json"


def generate_calendar_token(creds_file):
    print("\n========================================================")
    print("      Google Calendar Token Generator (Desktop Flow)    ")
    print("========================================================\n")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("❌ Error: google-auth-oauthlib is not installed.")
        print("Please install dependencies first: pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client")
        return

    scopes = ['https://www.googleapis.com/auth/calendar.readonly']
    print(f"Loading credentials from: {creds_file}")
    print("Starting authentication flow...")
    print("A browser window should open. If it doesn't, please click the link provided below.")
    
    try:
        flow = InstalledAppFlow.from_client_secrets_file(creds_file, scopes)
        creds = flow.run_local_server(port=0)
        
        Path(GOOGLE_TOKEN_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(GOOGLE_TOKEN_PATH, 'w') as token_file:
            token_file.write(creds.to_json())
            
        print(f"\n✅ Successfully generated {GOOGLE_TOKEN_PATH}!")
        print("Your Google Calendar integration is now ready to use.")
        print("\nNote: If your Google Cloud app's Publishing Status is set to 'Testing',")
        print("this token will expire in 7 days and you will need to run this script again.")
        print("To fix this permanently, change the status to 'In production' in the OAuth consent screen.")
    except Exception as e:
        print(f"\n❌ An error occurred during Calendar authentication: {e}")

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
        
    creds_file = str(CALENDAR_CREDENTIALS_PATH)
    if choice == "2" and NEST_CREDENTIALS_PATH.exists():
        creds_file = str(NEST_CREDENTIALS_PATH)
        
    if not os.path.exists(creds_file):
        print(f"❌ Error: Credentials file '{creds_file}' not found.")
        print("Please place your credentials JSON file under the secrets/ directory.")
        return
        
    if choice == "1":
        generate_calendar_token(creds_file)
    else:
        generate_nest_token(creds_file)

if __name__ == '__main__':
    main()
