' Reading Kit — RAG server autostart launcher (hidden window, survives logoff of the shell that started it).
' setup.ps1 fills the paths/port and puts this in the project root. Use it by:
'   - double-clicking to start the server manually, or
'   - copying into  shell:startup  so it starts with Windows, or
'   - setting it as the plugin's 自动启动命令:  wscript.exe "<项目根>\start_rag_server.vbs"
'
' Gotchas learned in production:
'   - Use python.exe, NOT pythonw.exe: rag.py prints at startup and pythonw has no
'     stdout — the server dies silently. Run style 0 hides the console anyway.
'   - If the server is already running the new instance exits on its own (port busy).
Set sh = CreateObject("WScript.Shell")
sh.Run """{{PYTHON_EXE}}"" ""{{PROJECT_ROOT}}\scripts\rag.py"" serve --port {{RAG_PORT}}", 0, False
