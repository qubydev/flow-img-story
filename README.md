# Flow Image Story

Flow Image desktop app in Python and PySide6, using your own Google Flow account and credits. No G-Labs account or license is needed. Each account uses a separate local Chrome profile.

## Run with the existing virtual environment

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Google Chrome must be installed. Click **Manage accounts** to view the account table, then **Add account**. An ordinary Chrome process opens Google's sign-in page; browser automation attaches only after Google returns to its home page. The app checks for a signed-in Google session and Flow access before closing Chrome and marking the account active. Enter one or more prompts (separate them by a blank line), choose model, ratio, count, and output folder, then click **Start generation** under the prompt. During a batch the button becomes **Stop generation**; stopping completes the current image request and skips the remaining prompts. Double-click completed tasks to open their output folder. Sessions are saved locally and can be exported or imported.

Flow's web RPC is undocumented and can change. A live generation has not yet been verified with a signed-in account in this workspace. Reference images, characters, upscale, and concurrent multi-account generation are not implemented yet.
