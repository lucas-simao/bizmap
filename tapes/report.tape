Output out/report.gif
Source tapes/common.tape
Set Height 480
Hide
Type "cd demo && export PS1='\[\e[1;32m\]bizmap-demo\[\e[0m\] $ ' && bizmap scan . -q >/dev/null && clear" Enter
Show
Sleep 500ms
Type "bizmap report" Sleep 300ms Enter
Sleep 6s
Screenshot out/report.png
Sleep 1s
