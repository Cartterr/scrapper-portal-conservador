' Windows-login bridge only: wait for Ubuntu and open the loopback dashboard in
' the native default browser once it is ready.
' All CBRS application processes remain managed by systemd inside Ubuntu.
Set shell = CreateObject("WScript.Shell")

dashboardReady = False
For attempt = 1 To 60
    checkCode = shell.Run("curl.exe -fsS --max-time 2 http://127.0.0.1:8765/", 0, True)
    If checkCode = 0 Then
        dashboardReady = True
        Exit For
    End If
    WScript.Sleep 1000
Next

If dashboardReady Then
    shell.Run "rundll32.exe url.dll,FileProtocolHandler http://127.0.0.1:8765/", 1, False
End If
