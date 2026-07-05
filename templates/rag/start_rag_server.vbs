' Reading Kit — RAG server autostart launcher (hidden window, survives logoff of the shell that started it).
' Copy to your project root, fix the two paths, then either:
'   - double-click to start the server manually, or
'   - copy into  shell:startup  so it starts with Windows, or
'   - set it as the plugin's 自动启动命令:  wscript.exe "X:\path\to\start_rag_server.vbs"
'
' Gotchas learned in production:
'   - Use python.exe, NOT pythonw.exe: rag.py prints at startup and pythonw has no
'     stdout — the server dies silently. Run style 0 hides the console anyway.
'   - If the server is already running the new instance exits on its own (port busy).
Set sh = CreateObject("WScript.Shell")
sh.Run """C:\Path\To\python.exe"" ""X:\Path\To\project\scripts\rag.py"" serve --port 8766", 0, False
