' Windows-login bridge only: keep Ubuntu WSL alive without a console window.
' All CBRS application processes remain managed by systemd inside Ubuntu.
Set shell = CreateObject("WScript.Shell")
shell.Run "wsl.exe -d Ubuntu-24.04 --exec /bin/sleep infinity", 0, False
