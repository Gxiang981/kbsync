' kbsync-watchdog.vbs
' Keeps the kbsync agent alive on Windows: checks every 30 s whether the
' agent process is alive and restarts it if it died.
' Put this file in the Startup folder (shell:startup) to have it run at logon.
' It must run in the interactive session - a service/session-0 process cannot
' read the cursor position and would never detect which machine you use.
'
' IMPORTANT: keep this file pure ASCII. cscript/wscript parse .vbs as ANSI,
' so any non-ASCII character (e.g. a Chinese comment) can break the script.
'
' Every helper command runs through cmd.exe with window style 0 (hidden) and
' its output redirected to a temp file. Shell.Exec would flash a console
' window on every check.
Option Explicit

Dim fso, sh, py, cfg, running, outText, ln, project, tmpDir, outFile

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
py = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe"
tmpDir = sh.ExpandEnvironmentStrings("%TEMP%")

Function RunHidden(cmdText)
    Dim res
    outFile = tmpDir & "\kbsync-wd-out.txt"
    On Error Resume Next
    If fso.FileExists(outFile) Then fso.DeleteFile outFile
    On Error GoTo 0
    sh.Run "cmd.exe /c " & cmdText & " > """ & outFile & """ 2>nul", 0, True
    res = ""
    On Error Resume Next
    If fso.FileExists(outFile) Then
        res = fso.OpenTextFile(outFile, 1).ReadAll
        fso.DeleteFile outFile
    End If
    On Error GoTo 0
    RunHidden = res
End Function

Function AgentRunning()
    Dim outText2, ln2
    AgentRunning = False
    outText2 = RunHidden("tasklist /FI ""IMAGENAME eq pythonw.exe"" /FO CSV /NH")
    For Each ln2 In Split(outText2, vbCrLf)
        If InStr(ln2, "kbsync.py") > 0 Then AgentRunning = True
    Next
End Function

Function FindProject()
    Dim item
    FindProject = ""
    For Each item In fso.GetFolder("F:\").SubFolders
        If Left(item.Name, 3) = "21_" Then
            If fso.FileExists(item.Path & "\kbsync.py") Then
                FindProject = item.Path
                Exit Function
            End If
        End If
    Next
End Function

Do While True
    project = FindProject()
    If project <> "" Then
        cfg = project & "\config.json"
        If Not AgentRunning() Then
            ' check once more before starting, so two watchdogs cannot spawn two agents
            WScript.Sleep 1500
            If Not AgentRunning() Then
                sh.Run """" & py & """ """ & project & "\kbsync.py"" --config """ & cfg & """", 0, False
            End If
        End If
    End If
    WScript.Sleep 30000
Loop
