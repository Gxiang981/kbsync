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
'
' Liveness check uses the agent's PID file (~/.kbsync/agent.pid) - tasklist
' in CSV mode does NOT include the command line, so filtering by "kbsync.py"
' there never matches (and would restart the agent every 30 s).
Option Explicit

Dim fso, sh, py, cfg, project, tmpDir, outFile, pidFile, agentPid, outText

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
py = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe"
tmpDir = sh.ExpandEnvironmentStrings("%TEMP%")
pidFile = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.kbsync\agent.pid"

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

' agent 是否在运行：读 PID 文件，再用 tasklist 按 PID 过滤（按 PID 过滤时会输出进程名）
Function AgentAlive()
    Dim pidText, outText2
    AgentAlive = False
    On Error Resume Next
    If Not fso.FileExists(pidFile) Then Exit Function
    pidText = Trim(fso.OpenTextFile(pidFile, 1).ReadAll)
    On Error GoTo 0
    If pidText = "" Then Exit Function
    If Not IsNumeric(pidText) Then Exit Function
    outText2 = RunHidden("tasklist /FI ""PID eq " & CLng(pidText) & """ /NH")
    If InStr(outText2, "python") > 0 Then AgentAlive = True
End Function

Do While True
    project = FindProject()
    If project <> "" Then
        cfg = project & "\config.json"
        If Not AgentAlive() Then
            ' 再确认一次，避免两个看门狗同时起两个 agent
            WScript.Sleep 1500
            If Not AgentAlive() Then
                sh.Run """" & py & """ """ & project & "\kbsync.py"" --config """ & cfg & """", 0, False
            End If
        End If
    End If
    WScript.Sleep 30000
Loop
