' ============================================================
'  Image Compressor Toolbox - GUI launcher (no console window)
'
'  Runs run_gui.bat with a HIDDEN cmd window, so double-clicking
'  this file starts the GUI with zero visible terminal flash.
'
'  Create a desktop shortcut to THIS file and you can also set
'  its icon to assets\app_icon.ico
'
'  NOTE: keep this file ASCII-only. VBS is parsed using the system
'  code page, so non-ASCII bytes can corrupt the syntax.
'  (run_gui.bat is deliberately GBK-encoded for the same reason.)
' ============================================================
Option Explicit

Dim app, fso, here, bat, args, q
Set fso = CreateObject("Scripting.FileSystemObject")

q = Chr(34)   ' double quote - avoids the """" escaping mess

here = fso.GetParentFolderName(WScript.ScriptFullName)
bat = here & "\run_gui.bat"

If Not fso.FileExists(bat) Then
    MsgBox "Cannot find launcher:" & vbCrLf & bat, 16, "Image Compressor Toolbox"
    WScript.Quit 1
End If

args = "/c " & q & bat & q

' ShellExecute sFile, vArguments, vDirectory, vOperation, vShow
'   vShow = 0 -> the cmd window stays hidden.
'   The GUI still appears normally, because run_gui.bat launches
'   it with the `start` command.
Set app = CreateObject("Shell.Application")
app.ShellExecute "cmd.exe", args, here, "", 0

Set app = Nothing
Set fso = Nothing
WScript.Quit 0
