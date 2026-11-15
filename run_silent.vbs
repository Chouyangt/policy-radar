' 静默运行抓取（无窗口），供 Windows 计划任务调用
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base
sh.Run "pythonw.exe """ & base & "\radar.py"" sync", 0, False
