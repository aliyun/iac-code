!macro NSIS_HOOK_POSTINSTALL
  ; Tauri's default shortcut inherits the executable icon without recording an
  ; icon location. Explorer can therefore retain the previous icon after an
  ; in-place upgrade. Point an existing shortcut at a separately bundled,
  ; versioned icon so its cache key changes. Do not create a shortcut when the
  ; user disabled desktop shortcuts in the installer.
  IfFileExists "$DESKTOP\${PRODUCTNAME}.lnk" 0 iac_code_refresh_shell
  IfFileExists "$INSTDIR\icons\iac-code-logo-v3.ico" 0 iac_code_refresh_shell
  CreateShortCut "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe" "" "$INSTDIR\icons\iac-code-logo-v3.ico" 0

  ; Notify Explorer that icon-bearing shell items changed without restarting it.
  iac_code_refresh_shell:
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
!macroend
