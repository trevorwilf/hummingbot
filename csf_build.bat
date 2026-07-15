@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat" x64
C:/anaconda3/envs/hummingbot/python.exe setup.py build_ext --inplace -j 8
