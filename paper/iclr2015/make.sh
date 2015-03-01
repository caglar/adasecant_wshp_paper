#!/bin/bash -x
#rm *.bbl
rm *.log
#rm *.blg
rm submission_iclr.pdf 

pdflatex submission_iclr.tex
bibtex submission_iclr
pdflatex submission_iclr.tex
