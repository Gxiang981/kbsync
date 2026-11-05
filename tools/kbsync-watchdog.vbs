' kbsync-watchdog.vbs
' Keeps the kbsync agent alive on Windows: checks every 30 s whether the
' agent process is alive and restarts it if it died.
' Put this file in the Startup folder (shell:startup) to have it run at logon.
' It must run in the interactive session - a service/session-0 process cannot
' read the cursor position and would never detect which machine you use.
Option Explicit

Dim fso, sh, py, cfg, running, outText, ln, project
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
py = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe"

Function FindProject()
    Dim folder
    FindProject = ""
    For Each folder In fso.GetFolder("F:\").SubFolders
        If Left(folder.Name, 3) = "21_" Then
            If fso.FileExists(folder.Path & "\kbsync.py") Then
                FindProject = folder.Path
                Exit Function
            End If
        End If
    Next
End Function

' 单例：已经有别的看门狗在跑就直接退出，避免重复启动多个 agent
Dim selfText, otherText, otherLine, dupFound
dupFound = False
selfText = sh.Exec("tasklist /FI ""IMAGENAME eq wscript.exe"" /FO CSV /NH").StdOut.ReadAll
For Each otherLine In Split(selfText, vbCrLf)
    If InStr(otherLine, "kbsync-watchdog.vbs") > 0 Then
        ' tasklist 只有进程名，取不到命令行；用 wmic 精确查一次
        otherText = sh.Exec("wmic process where ""name='wscript.exe'"" get commandline /value").StdOut.ReadAll
        If (Len(otherText) - Len(Replace(otherText, "kbsync-watchdog.vbs", ""))) / Len("kbsync-watchdog.vbs") > 1 Then
            dupFound = True
        End If
    End If
Next
If dupFound Then
    WScript.Quit 0
End If

Do While True
    project = FindProject()
    If project <> "" Then
        cfg = project & "\config.json"
        running = False
        outText = sh.Exec("tasklist /FI ""IMAGENAME eq pythonw.exe"" /FO CSV /NH").StdOut.ReadAll
        For Each ln In Split(outText, vbCrLf)
            If InStr(ln, "kbsync.py") > 0 Then running = True
        Next
        If Not running Then
            sh.Run """" & py & """ """ & project & "\kbsync.py"" --config """ & cfg & """", 0, False
        End If
    End If
    WScript.Sleep 30000
Loop
