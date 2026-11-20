Output out/scan.gif
Source tapes/common.tape
Set Height 300
Hide
Type "cd demo && export PS1='\[\e[1;32m\]bizmap-demo\[\e[0m\] $ ' && clear" Enter
Show
Sleep 500ms
Type "bizmap scan . --mermaid" Sleep 300ms Enter
Sleep 2s
Type "bizmap scan shop --out /tmp/shop.json" Sleep 300ms Enter
Sleep 4s
Screenshot out/scan.png
Sleep 1s
