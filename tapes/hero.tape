Output out/hero.gif
Source tapes/common.tape
Set Height 700
Hide
Type "cd demo && export PS1='\[\e[1;32m\]bizmap-demo\[\e[0m\] $ ' && clear" Enter
Show
Sleep 500ms
Type "bizmap scan . --mermaid" Sleep 300ms Enter
Sleep 2s
Type "bizmap why orders.status" Sleep 300ms Enter
Sleep 6s
Screenshot out/hero.png
Sleep 1s
