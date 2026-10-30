"""Public URLs the app links to (About pages, Settings → Accounts).

The site (homepage, privacy policy, terms) is site/ in this repo, deployed to
Cloudflare Pages by the `site` CI job. Google's OAuth verification requires
the privacy policy on the verified domain redcyberdragon.net.
"""

SITE = "https://chattersyn.redcyberdragon.net"
PRIVACY_POLICY = f"{SITE}/privacy"
TERMS = f"{SITE}/terms"
SOURCE = "https://github.com/smitht06/red-chattersyn"

# YouTube API Services require these in apps that use them.
YOUTUBE_TERMS = "https://www.youtube.com/t/terms"
GOOGLE_PRIVACY = "https://policies.google.com/privacy"

# Where users revoke ChatterSyn's access.
GOOGLE_CONNECTIONS = "https://myaccount.google.com/connections"
TWITCH_CONNECTIONS = "https://www.twitch.tv/settings/connections"
