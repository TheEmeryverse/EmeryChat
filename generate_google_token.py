import os
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

def load_env_custom():
    if os.path.exists('.env'):
        with open('.env', 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    parts = line.split('=', 1)
                    key = parts[0].strip()
                    val = parts[1].split('#')[0].strip()
                    if val.startswith('"') and val.endswith('"'):
                        val = val[1:-1]
                    elif val.startswith("'") and val.endswith("'"):
                        val = val[1:-1]
                    os.environ[key] = val

def main():
    load_env_custom()
    
    # Prioritize nest_credentials.json if present
    creds_file = 'nest_credentials.json'
    if not os.path.exists(creds_file):
        creds_file = 'credentials.json'
        
    if not os.path.exists(creds_file):
        print("❌ Error: credentials.json or nest_credentials.json not found.")
        print("Please place your credentials JSON file in this directory.")
        return
        
    print(f"Loading credentials from: {creds_file}")
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
        
    nest_project_id = os.getenv("NEST_PROJECT_ID")
    if not nest_project_id or nest_project_id.strip() == "" or "YOUR_" in nest_project_id:
        print("❌ Error: NEST_PROJECT_ID is not configured in your .env file.")
        print("Please add your Nest Device Access Project ID (UUID) to your .env file as NEST_PROJECT_ID=...")
        return
        
    print("\n========================================================")
    print("      Google Nest Token Generator Helper (PCM Flow)      ")
    print("========================================================\n")
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
            
            with open('nest_token.json', 'w') as token_file:
                json.dump(token_json, token_file)
                
            print("\n✅ Successfully generated nest_token.json!")
            print("Your Google Nest Thermostat integration is now ready to use.")
            
    except Exception as e:
        print(f"\n❌ Failed to exchange code for token: {e}")
        if hasattr(e, 'read'):
            print("Response details:", e.read().decode('utf-8'))

if __name__ == '__main__':
    main()
