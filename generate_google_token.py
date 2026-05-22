import os
from google_auth_oauthlib.flow import InstalledAppFlow

# The SCOPES should match what the application requests
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

def main():
    if not os.path.exists('credentials.json'):
        print("❌ Error: credentials.json not found.")
        print("Please download it from the Google Cloud Console (APIs & Services -> Credentials) and place it in this directory.")
        return
        
    print("Starting authentication flow...")
    print("A browser window should open. If it doesn't, please click the link provided below.")
    
    try:
        flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
        creds = flow.run_local_server(port=0)
        
        with open('token.json', 'w') as token_file:
            token_file.write(creds.to_json())
            
        print("\n✅ Successfully generated token.json!")
        print("Your Google Calendar integration is now ready to use.")
        print("\nNote: If your Google Cloud app's Publishing Status is set to 'Testing',")
        print("this token will expire in 7 days and you will need to run this script again.")
        print("To fix this permanently, change the status to 'In production' in the OAuth consent screen.")
        
    except Exception as e:
        print(f"\n❌ An error occurred during authentication: {e}")

if __name__ == '__main__':
    main()
