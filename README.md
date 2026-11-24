# ItemFinder

ItemFinder finds duplicate items in your photo library, even when the file names or item codes are different. You can then rename, move or delete the extra copies. It runs 100% offline: a local AI model does the matching, and photos never leave the PC.

---

## Part 1: One-time setup (publisher)

GitHub builds the Windows installer for you, so you don't need Python or build tools on your own PC.

1. **Create a GitHub repository.** On github.com, go to **New repository**, name it `ItemFinder` and make it **Public**. Updates download from the repo's Releases page, so a public repo means the app can fetch them without a password. *(Want to keep the code private? See "Private code" below.)*
2. **Upload these files.** On the repo page, click **Add file → Upload files**, then drag in everything from this folder.
   - Include the hidden `.github` folder. In Windows Explorer, turn on **View → Show → Hidden items**.
   - If the `.github` folder doesn't upload, use **Add file → Create new file**. Name it `.github/workflows/release.yml` and paste in the file's contents.
3. **Publish version 1.0.0.** Open the **Actions** tab and choose **Build & release installer**. Click **Run workflow**, type `1.0.0` and run it. The build takes about 20–30 minutes.
4. **Download the installer.** When the build finishes, the **Releases** page has `ItemFinder-1.0.0-Setup.exe`. Share that file with your users.

## Part 2: Publishing an update

1. Change the code. If you want the update to show something new, also edit `RELEASE_NOTES.md`; users see this text in the update prompt.
2. Open **Actions**, choose **Build & release installer** and click **Run workflow**. Enter a **higher** version number, for example `1.0.1`.
3. That's it. Every installed copy checks for updates about once a day and offers the new version. Users can also click **Check for updates** in the app.

What happens on the user's PC when they click **Update now**:

1. The app downloads the new installer.
2. It checks the file's SHA-256 checksum and rejects anything that doesn't match.
3. The app closes, the installer updates it silently with no admin prompt, and the app reopens.

The photo index is kept, and indexing that was in progress resumes on its own.

If a build breaks the app, the workflow's self-test step fails and **nothing is published**.

---

## Installing (users)

1. Download `ItemFinder-x.y.z-Setup.exe` and run it. No admin rights are needed; the app installs for the current user only.
2. The first time, Windows may show **"Windows protected your PC"** because the installer isn't code-signed. Click **More info → Run anyway**. This only happens on the first install, not on auto-updates.
3. On first launch, the app downloads its AI model once (about 600 MB). After that it works without internet; internet is only used to check for updates.
4. Choose your photo folder(s). The first indexing takes roughly 30–60 minutes for 50,000 photos. You can cancel at any time, and it resumes where it stopped.

**Uninstalling:** go to **Settings → Apps → ItemFinder → Uninstall**. The uninstaller asks whether to also delete the photo index. Your photos are never touched.

## Using it

- **Find duplicates:**
  1. Pick a match level and click **Find duplicates**.
  2. Click a group on the left to see its photos.
  3. Click **Tick all but best copy**. This keeps the highest-resolution photo and ticks the rest.
  4. Click **Delete** (sends them to the Recycle Bin), **Move to…** or **Rename…**.
- **Renaming several photos at once:** tick several photos and click **Rename…**. They get numbered names, for example `BLUE-TURTLE_01.jpg`, `BLUE-TURTLE_02.jpg`.
- **Search by photo:**
  - Drag a photo onto the window, click **Browse**, or press Ctrl+V to paste a copied image.
  - You can add words to steer the search, for example "blue".
- **Shortcuts:**
  - Del: delete
  - F2: rename
  - Enter: open
  - Right-click: show in folder, or find items like this one

## Good to know

- **Where data is kept:** the index and AI model are in `%LOCALAPPDATA%\ItemFinder`. The app itself is in `%LOCALAPPDATA%\Programs\ItemFinder`.
- **PCs with no internet:** run the app once on a PC that has internet. Then copy `%LOCALAPPDATA%\ItemFinder\models\` to the same location on the offline PC.
- **Update size:** each update downloads the full installer (a few hundred MB, mostly the AI runtime). The AI model itself is not downloaded again.
- **Private code:** keep this repo private and create a second, **public** repo (for example `ItemFinder-releases`) that only holds the installers. Then, in the private repo:
  1. Go to **Settings → Secrets and variables → Actions → Variables** and add `RELEASE_REPO` = `yourname/ItemFinder-releases`.
  2. Under **Secrets**, add `RELEASE_TOKEN`: a fine-grained GitHub token with *Contents: read & write* access to the releases repo.
- **Code signing (optional):** signing removes the SmartScreen warning. Buy a code-signing certificate, then add the repo secrets `SIGN_PFX_BASE64` (your .pfx file, base64-encoded) and `SIGN_PFX_PASSWORD`. The workflow signs the installer automatically.
- **Publisher name:** the installer shows "Temple & Pixel". To change it, edit `MyAppPublisher` in `installer/ItemFinder.iss`.
- **Running from source (developers):** `run.bat` sets up a Python environment and starts the app. Auto-update is disabled in this mode.

## Files

| File | Purpose |
|---|---|
| `app.py` | The window and all user actions |
| `engine.py` | AI model, photo index, duplicate finder, file actions |
| `updater.py`, `version.py` | Auto-update (the workflow fills in `version.py`) |
| `ItemFinder.spec` | Packages the app into `ItemFinder.exe` |
| `installer/ItemFinder.iss` | Windows installer (Inno Setup) |
| `.github/workflows/release.yml` | Builds, self-tests and publishes each release |
| `assets/itemfinder.ico` | App icon |
| `run.bat`, `build_exe.bat` | Run from source or build locally (optional) |
