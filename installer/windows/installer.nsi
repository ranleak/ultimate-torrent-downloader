; Ultimate Torrent Downloader - Production NSIS Modern UI 2 Installer
; Cross-compiled on Linux using `makensis`

Unicode True
SetCompressor /SOLID lzma

!ifndef APP_VERSION
  !define APP_VERSION "0.1.0"
!endif
!ifndef APP_VI_VERSION
  !define APP_VI_VERSION "0.1.0.0"
!endif
!ifndef STAGE_DIR
  !error "STAGE_DIR must be defined via -DSTAGE_DIR=..."
!endif
!ifndef OUTPUT_EXE
  !error "OUTPUT_EXE must be defined via -DOUTPUT_EXE=..."
!endif
!ifndef MANIFEST_DIR
  !error "MANIFEST_DIR must be defined via -DMANIFEST_DIR=..."
!endif

!define APP_NAME "Ultimate Torrent Downloader"
!define APP_PUBLISHER "ranleak"
!define APP_URL "https://github.com/ranleak/ultimate-torrent-downloader"
!define UNINSTALL_REG_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\UltimateTorrentDownloader"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "${OUTPUT_EXE}"
InstallDir "$LOCALAPPDATA\UltimateTorrentDownloader"
InstallDirRegKey HKCU "${UNINSTALL_REG_KEY}" "InstallLocation"
RequestExecutionLevel user
ShowInstDetails show
ShowUnInstDetails show

VIProductVersion "${APP_VI_VERSION}"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey "LegalCopyright" "MIT License - Copyright (c) 2026 ranleak"
VIAddVersionKey "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"

!include "MUI2.nsh"
!include "FileFunc.nsh"

!define MUI_ABORTWARNING
!define MUI_ICON "${STAGE_DIR}/app.ico"
!define MUI_UNICON "${STAGE_DIR}/app.ico"

!define MUI_FINISHPAGE_RUN "$INSTDIR\python\pythonw.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS "-m utd.client"
!define MUI_FINISHPAGE_RUN_TEXT "Launch ${APP_NAME}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${MANIFEST_DIR}/LICENSE.txt"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Section "Application" SecMain
  SetOutPath "$INSTDIR"

  ; Generated deterministically by scripts/build_windows_installer.py
  !include "${MANIFEST_DIR}/files_install.nsh"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; Start Menu & Desktop shortcuts
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  SetOutPath "$INSTDIR"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" \
    "$INSTDIR\python\pythonw.exe" "-m utd.client" "$INSTDIR\app.ico" 0
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME} (Console).lnk" \
    "$INSTDIR\python\python.exe" "-m utd.client" "$INSTDIR\app.ico" 0
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" \
    "$INSTDIR\Uninstall.exe" "" "$INSTDIR\app.ico" 0
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" \
    "$INSTDIR\python\pythonw.exe" "-m utd.client" "$INSTDIR\app.ico" 0

  ; Add/Remove Programs metadata (per-user HKCU)
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0

  WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "URLInfoAbout" "${APP_URL}"
  WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "DisplayIcon" "$INSTDIR\app.ico"
  WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_REG_KEY}" "EstimatedSize" "$0"
  WriteRegDWORD HKCU "${UNINSTALL_REG_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_REG_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME} (Console).lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"

  !include "${MANIFEST_DIR}/files_uninstall.nsh"

  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${UNINSTALL_REG_KEY}"
SectionEnd
