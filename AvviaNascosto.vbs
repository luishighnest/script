On Error Resume Next

' ==============================================================================
' Avvio Ottimizzato Script2 - Chiusura processi orfani, tunnel e aggiornamento
' ==============================================================================

base    = "C:\Users\alecl\Desktop\PROJECT_HUB\core_scripts\script2\"
py      = "C:\Users\alecl\AppData\Local\Programs\Python\Python313\pythonw.exe"
cf      = "C:\Users\alecl\AppData\Local\Temp\opencode\cloudflared.exe"
logfile = "C:\Users\alecl\AppData\Local\Temp\opencode\cf_bat.log"

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
Set wmi = GetObject("winmgmts:\\.\root\cimv2")

' 1) TERMINAZIONE PULITA DI TUTTI I PROCESSI PRECEDENTI
' Termina qualsiasi istanza precedente di cloudflared
For Each proc In wmi.ExecQuery("Select * from Win32_Process Where Name = 'cloudflared.exe'")
    proc.Terminate()
Next

' Termina processi python che eseguono app.py o script2
For Each proc In wmi.ExecQuery("Select * from Win32_Process Where Name = 'python.exe' or Name = 'pythonw.exe'")
    cmdLine = LCase(proc.CommandLine)
    If InStr(cmdLine, "app.py") > 0 Or InStr(cmdLine, "script2") > 0 Then
        proc.Terminate()
    End If
Next

' Libera la porta 5000 se ancora occupata
sh.Run "cmd /c ""for /f ""tokens=5"" %a in ('netstat -aon ^| findstr "":5000"" ^| findstr ""LISTENING""') do taskkill /f /pid %a""", 0, True

' Breve pausa per rilascio socket di rete
WScript.Sleep 1000

' 2) Pulizia log precedente del tunnel
If fso.FileExists(logfile) Then fso.DeleteFile logfile

' 3) Avvio Flask nascosto su localhost
sh.CurrentDirectory = base
sh.Run """" & py & """ app.py", 0, False

' Attende che Flask si inizializzi
WScript.Sleep 2500

' 4) Avvio Tunnel Cloudflare puntando a IPv4 127.0.0.1 (evita conflitti IPv6 / 502)
sh.Run """" & cf & """ tunnel --url http://127.0.0.1:5000 --no-autoupdate --logfile """ & logfile & """ --loglevel info", 0, False

' 5) Polling dinamico per il link del tunnel (fino a 30 secondi)
link = ""
For t = 1 To 30
    WScript.Sleep 1000
    If fso.FileExists(logfile) Then
        Set f = fso.OpenTextFile(logfile, 1)
        Do Until f.AtEndOfStream
            line = f.ReadLine
            p = InStr(line, "https://")
            If p > 0 Then
                resto = Mid(line, p)
                sp = InStr(resto, " ")
                If sp > 0 Then resto = Left(resto, sp - 1)
                resto = Replace(resto, """", "")
                resto = Replace(resto, "|", "")
                resto = Replace(resto, vbCr, "")
                resto = Replace(resto, vbLf, "")
                If InStr(resto, "trycloudflare.com") > 0 Then
                    link = Trim(resto)
                    Exit Do
                End If
            End If
        Loop
        f.Close
        If link <> "" Then Exit For
    End If
Next

' 6) Aggiorna il redirect su GitHub Pages se il link è valido
If link <> "" Then
    sh.Run """" & py & """ update_redirect.py """ & link & """", 0, True
    ' Notifica informativa non bloccante (scompare da sola dopo 4 secondi)
    sh.Popup "Script2 avviato con successo!" & vbCrLf & vbCrLf & _
             "Link fisso: https://luishighnest.github.io/script2/" & vbCrLf & _
             "Link Cloudflare: " & link, 4, "Script2 Online", 64
Else
    sh.Popup "Attenzione: Impossibile recuperare il link Cloudflare." & vbCrLf & _
             "Verifica la connessione internet e controlla: " & logfile, 6, "Script2 Errore Avvio", 48
End If