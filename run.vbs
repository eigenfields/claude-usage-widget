' Silent launcher - starts the widget with no console flash (pyw/pythonw = GUI python).
' Double-click this, or drop a shortcut to it in shell:startup to auto-run at login.
Set s = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
s.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
On Error Resume Next
s.Run "pyw.exe app.py", 0, False        ' 0 = hidden launcher window, False = don't wait
If Err.Number <> 0 Then                 ' no py launcher -> try pythonw on PATH
    Err.Clear
    s.Run "pythonw.exe app.py", 0, False
End If
