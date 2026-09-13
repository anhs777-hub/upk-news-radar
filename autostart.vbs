' 뉴스 레이더 서버 부팅 자동 기동 — 콘솔 없이 server.py(8091) 실행.
'
' 설계 요점:
'   1) 중복 기동 가드를 HTTP 헬스체크로 한다. 예전엔 powershell -Command 로
'      Get-NetTCPConnection을 돌렸는데, 부팅 직후 powershell 콜드 스타트가
'      수십 초 걸리는 데다 모듈 로드 실패 등으로 exit 1이 나오면 "이미 떠 있음"
'      으로 오판해 서버를 아예 안 띄웠다(조용한 실패). MSXML2로 직접 찌르면
'      외부 프로세스 없이 즉시 판정되고, 포트만 열린 좀비도 걸러진다.
'   2) venv의 pythonw 사용 — 콘솔 창 없음 + 의존성 격리. 없으면 PATH 폴백.
'      (server.py는 pythonw에서 stdout/stderr=None 방어가 되어 있다.)
'   3) 기동 후 최대 40초까지 헬스체크로 확인하고 결과를 data\_autostart.log 에
'      남긴다. 자동 기동이 또 실패하면 이 로그의 유무로 "vbs가 안 돌았는지"와
'      "돌았는데 서버가 안 떴는지"를 구분할 수 있다.

Option Explicit

Dim WshShell, fso, qdir, logPath, pyw, i, ok
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

qdir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = qdir
logPath = qdir & "\data\_autostart.log"

WriteLog "--- autostart start (cwd=" & qdir & ") ---"

' --- 1) 이미 응답하면 아무것도 하지 않는다 ---
If ServerAlive() Then
    WriteLog "already alive - skip"
    WScript.Quit
End If

' --- 2) venv pythonw 우선, 없으면 PATH pythonw ---
pyw = qdir & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pyw) Then
    WriteLog "venv pythonw missing - fallback to PATH pythonw"
    pyw = "pythonw"
End If

' --- 3) 콘솔 없이 server.py --port 8091 실행 ---
On Error Resume Next
WshShell.Run """" & pyw & """ server.py --port 8091", 0, False
If Err.Number <> 0 Then
    WriteLog "launch FAILED: " & Err.Number & " " & Err.Description
    Err.Clear
    On Error GoTo 0
    WScript.Quit
End If
On Error GoTo 0
WriteLog "launched: " & pyw

' --- 4) 실제로 떴는지 최대 40초 확인 ---
ok = False
For i = 1 To 20
    WScript.Sleep 2000
    If ServerAlive() Then
        ok = True
        Exit For
    End If
Next

If ok Then
    WriteLog "health OK after " & (i * 2) & "s"
Else
    WriteLog "health FAILED after 40s - server did not come up"
End If


' ===== helpers =====

' 8091이 실제로 응답하는지. 포트만 열고 죽은 좀비는 False로 친다.
Function ServerAlive()
    Dim http
    ServerAlive = False
    On Error Resume Next
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        Exit Function
    End If
    http.setTimeouts 2000, 2000, 2000, 3000
    http.open "GET", "http://127.0.0.1:8091/api/health", False
    http.send
    If Err.Number = 0 Then
        If http.status = 200 Then ServerAlive = True
    End If
    Err.Clear
    On Error GoTo 0
End Function

' data\ 가 아직 없을 수 있으므로 만들고 append. 로그 실패가 기동을 막으면 안 된다.
Sub WriteLog(msg)
    Dim f, dataDir
    On Error Resume Next
    dataDir = qdir & "\data"
    If Not fso.FolderExists(dataDir) Then fso.CreateFolder dataDir
    Set f = fso.OpenTextFile(logPath, 8, True)
    f.WriteLine Now & "  " & msg
    f.Close
    Err.Clear
    On Error GoTo 0
End Sub
