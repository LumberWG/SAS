' ============================================================
'  SAS minute-bar export launcher
'  Double-click to start exporting 5m bars, then 1m bars.
'  Runs hidden and detached: closing anything here will NOT stop it.
'  If the job dies (e.g. db service dropped), it is retried up to 6 times.
'  Log: D:\Backup\SAS\data\logs\minutes_all.log
' ============================================================

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

workDir = "E:\Study\SAS\backend"
pyExe   = workDir & "\.venv\Scripts\python.exe"
script  = workDir & "\scripts\run_minutes_all.py"
logFile = "D:\Backup\SAS\data\logs\minutes_all.log"

If Not fso.FileExists(pyExe) Then
    MsgBox "python.exe not found:" & vbCrLf & pyExe, 16, "SAS Export"
    WScript.Quit 1
End If

If Not fso.FileExists(script) Then
    MsgBox "script not found:" & vbCrLf & script, 16, "SAS Export"
    WScript.Quit 1
End If

logDir = fso.GetParentFolderName(logFile)
If Not fso.FolderExists(logDir) Then fso.CreateFolder logDir

sh.CurrentDirectory = workDir

cmd = "cmd /c """"" & pyExe & """ """ & script & """ >> """ & logFile & """ 2>&1"""

MsgBox "Export started in background." & vbCrLf & vbCrLf & _
       "  Step 1: 5-minute bars (resumes automatically)" & vbCrLf & _
       "  Step 2: 1-minute bars (starts when step 1 finishes)" & vbCrLf & _
       "  Step 3: refill failed codes" & vbCrLf & vbCrLf & _
       "Log file:" & vbCrLf & logFile & vbCrLf & vbCrLf & _
       "You can close this dialog. The job keeps running.", _
       64, "SAS Export Running"

' 0 = hidden window, True = wait for exit code so we can retry on failure
For attempt = 1 To 6
    rc = sh.Run(cmd, 0, True)
    If rc = 0 Then Exit For
    ' non-zero means the job died midway (db dropped / interrupted);
    ' wait 30s then resume; the export script skips finished codes
    WScript.Sleep 30000
Next
