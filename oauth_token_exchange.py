"""
OAuth2 Token Exchange for Upstox.

Handles the daily token refresh cycle:
1. Generates OAuth2 login URL
2. Prompts user to authorize and paste the authorization code
3. Exchanges code for fresh access_token
4. Updates .env with new token

Run once every morning at 9:00 AM IST.
"""

import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv


class UpstoxTokenExchange:
    """OAuth2 token exchange for Upstox API."""
    
    AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
    TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
    
    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv("UPSTOX_API_KEY", "").strip()
        self.api_secret = os.getenv("UPSTOX_API_SECRET", "").strip()
        self.redirect_uri = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:8080/callback")
        self.env_path = Path(".env")
    
    def validate_credentials(self) -> bool:
        """Check if API credentials are configured."""
        if not self.api_key or self.api_key == "your_api_key_here":
            print("[ERROR] UPSTOX_API_KEY not set in .env")
            return False
        if not self.api_secret or self.api_secret == "your_api_secret_here":
            print("[ERROR] UPSTOX_API_SECRET not set in .env")
            return False
        return True
    
    def generate_login_url(self) -> str:
        """Generate the OAuth2 login URL."""
        scope = "default"
        req_params = {
            "client_id": self.api_key,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": scope,
        }
        
        # Build URL manually
        params_str = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in req_params.items())
        return f"{self.AUTH_URL}?{params_str}"
    
    def get_authorization_code(self) -> str:
        """Prompt user to authorize and paste the authorization code."""
        print("\n" + "="*80)
        print("UPSTOX OAUTH2 LOGIN REQUIRED")
        print("="*80)
        
        login_url = self.generate_login_url()
        print(f"\n1. Copy this link and open in your browser:\n")
        print(f"   {login_url}\n")
        
        print("2. Log in with your phone number and TOTP (2FA code)")
        print("3. You will be redirected to a page (might show 'localhost refused')")
        print("4. Look at the URL bar and copy the 'code=...' part\n")
        print("-" * 80)
        
        while True:
            code = input("Paste the authorization code from URL: ").strip()
            
            if len(code) < 10:
                print("[ERROR] Code too short. Try again.")
                continue
            
            # Remove 'code=' prefix if user accidentally included it
            if code.startswith("code="):
                code = code[5:]
            
            # Validate: authorization codes are typically alphanumeric + special chars
            if re.match(r'^[a-zA-Z0-9\-_.~]+$', code):
                print(f"[OK] Authorization code: {code[:20]}...")
                return code
            else:
                print("[ERROR] Invalid code format. Try again.")
    
    def exchange_code_for_token(self, auth_code: str) -> str | None:
        """Exchange authorization code for access token."""
        print("\n[*] Exchanging authorization code for access token...")
        
        token_request = {
            "code": auth_code,
            "client_id": self.api_key,
            "client_secret": self.api_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }
        
        try:
            response = requests.post(self.TOKEN_URL, json=token_request, timeout=10)
            
            if response.status_code != 200:
                print(f"[ERROR] Token exchange failed: HTTP {response.status_code}")
                print(f"Response: {response.text}")
                return None
            
            token_data = response.json()
            access_token = token_data.get("access_token")
            expires_in = token_data.get("expires_in", "unknown")
            
            if not access_token:
                print(f"[ERROR] No access_token in response: {token_data}")
                return None
            
            print(f"[✓] Access token received (expires in {expires_in}s)")
            print(f"[✓] Token: {access_token[:50]}...")
            return access_token
        
        except requests.RequestException as e:
            print(f"[ERROR] Network request failed: {e}")
            return None
    
    def update_env_file(self, new_token: str) -> bool:
        """Update .env with new access token."""
        try:
            if not self.env_path.exists():
                print(f"[ERROR] .env file not found at {self.env_path}")
                return False
            
            with open(self.env_path, "r") as f:
                lines = f.readlines()
            
            # Find and replace access token line
            updated = False
            for i, line in enumerate(lines):
                if line.startswith("UPSTOX_ACCESS_TOKEN="):
                    lines[i] = f"UPSTOX_ACCESS_TOKEN={new_token}\n"
                    updated = True
                    break
            
            if not updated:
                # Add token if not present
                lines.append(f"UPSTOX_ACCESS_TOKEN={new_token}\n")
            
            with open(self.env_path, "w") as f:
                f.writelines(lines)
            
            print(f"[✓] Updated .env with new token")
            return True
        
        except Exception as e:
            print(f"[ERROR] Failed to update .env: {e}")
            return False
    
    def refresh_token(self) -> str | None:
        """
        Complete OAuth2 flow: Login → Code → Token → Save.
        Returns: new access_token (or None on failure)
        """
        if not self.validate_credentials():
            print("\n[ACTION] Add these to .env:")
            print("  UPSTOX_API_KEY=<your_key>")
            print("  UPSTOX_API_SECRET=<your_secret>")
            print("\nGet them from: https://upstox.com/developer/apps")
            return None
        
        # Step 1: Get authorization code from user
        auth_code = self.get_authorization_code()
        
        # Step 2: Exchange code for access token
        access_token = self.exchange_code_for_token(auth_code)
        if not access_token:
            return None
        
        # Step 3: Save to .env
        if not self.update_env_file(access_token):
            return None
        
        print("\n[✓] OAuth2 flow complete. Token ready for use.")
        return access_token


def get_fresh_access_token() -> str | None:
    """
    Public API: Refreshes access token and returns it.
    Call this at script startup.
    """
    exchanger = UpstoxTokenExchange()
    return exchanger.refresh_token()


if __name__ == "__main__":
    # Debug/test: Run token exchange manually
    token = get_fresh_access_token()
    if token:
        print(f"\n✓ New token: {token[:50]}...")
    else:
        print("\n✗ Token refresh failed")
