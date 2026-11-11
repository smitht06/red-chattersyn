## Purpose

Defines how ChatterSyn protects the data it keeps on the user's machine: the
settings file, stored credentials, and downloaded images.

Terms: the **settings file** is `settings.json` in ChatterSyn's config folder; the
**backup** is `settings.json.bak` next to it; the **plaintext file** is the
`secrets.json` credential fallback.

## ADDED Requirements

### Requirement: Settings survive a damaged file
Every save of the settings file SHALL first keep the previous settings file as the
backup. When the settings file can't be read at launch, ChatterSyn SHALL rename it
to `settings.json.corrupt-<YYYYmmdd-HHMMSS>`, load the backup, and tell the user
"Your settings file was damaged — restored the last good copy.". A damaged
settings file SHALL never be overwritten.

#### Scenario: LD-1 backup kept on save
- **WHEN** settings are saved while a settings file exists
- **THEN** the previous file's content is in `settings.json.bak`

#### Scenario: LD-2 damaged file restored from the backup
- **WHEN** ChatterSyn starts and settings.json is not valid JSON but the backup is
- **THEN** the backup's settings (including the tabs) are loaded, the damaged file
  is kept as `settings.json.corrupt-<timestamp>`, and the user sees "Your settings
  file was damaged — restored the last good copy."

### Requirement: Damaged settings without a backup
When neither the settings file nor the backup can be read, ChatterSyn SHALL start
with default settings, keep the damaged file as
`settings.json.corrupt-<YYYYmmdd-HHMMSS>`, and tell the user "Your settings file
was damaged and no backup could be read — started with default settings.".

#### Scenario: LD-3 no usable backup
- **WHEN** both settings.json and the backup can't be read
- **THEN** ChatterSyn starts with defaults, the damaged file is kept as
  `settings.json.corrupt-<timestamp>`, and the user sees "Your settings file was
  damaged and no backup could be read — started with default settings."

#### Scenario: LD-4 first launch
- **WHEN** no settings file exists
- **THEN** ChatterSyn starts with defaults and shows no message

### Requirement: No plaintext credentials left behind
At startup, when the system keyring works and the plaintext file exists, ChatterSyn
SHALL move every entry of the plaintext file into the keyring and then delete the
file. When any entry can't be stored in the keyring, the plaintext file SHALL stay
unchanged.

#### Scenario: LD-5 keyring works again
- **WHEN** ChatterSyn starts with a working keyring and a secrets.json holding a
  Twitch login
- **THEN** the Twitch login is read from the keyring and secrets.json no longer
  exists

#### Scenario: LD-6 keyring refuses an entry
- **WHEN** the keyring refuses to store one of the entries
- **THEN** secrets.json is left exactly as it was

### Requirement: Image size cap
ChatterSyn SHALL not decode or show a downloaded image larger than 8 MB (8 × 1024
× 1024 bytes); such an image is skipped and logged, and other images keep loading.

#### Scenario: LD-7 oversized image
- **WHEN** an emote URL returns a 9 MB body
- **THEN** that emote isn't shown, nothing is decoded, and the log names the URL

#### Scenario: LD-8 normal image
- **WHEN** an emote URL returns a 200 KB image
- **THEN** the emote is shown as before
