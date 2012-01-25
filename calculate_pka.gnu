set term gif size 1280,1024 enhanced font 'calibri,20'
set output 'ASP_titration_curve.gif'

a = 1e-5
f(x) = 1 / (a * 10 ** x + 1)

fit f(x) 'ASP_titration_curve.dat' u 1:2 via a

set title "ASP Titration Curve (igb=8)" font 'calibri,25'
set xl "pH" font 'calibri,20'
set yl "Fraction Protonated" font 'calibri,20'
#unset key

plot 'ASP_titration_curve.dat' w p pt 4 ps 2 title '', f(x) w l lw 2 lt -1 title sprintf('pK_a = %.4g',-log10(a))
