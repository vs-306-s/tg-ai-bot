' Запуск бота без окна консоли (удобно положить ярлык в автозагрузку).
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
shell.Run "cmd /c """ & folder & "\start_bot.bat""", 0, False
