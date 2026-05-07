;
; MML ONE Resolve Sync — Windows installer (NSIS).
; Public repo flat layout: MMLOneSync.py + mml_sync/ at repo root.
;
; Build:  makensis installer.nsi
; Output: ..\..\dist\windows\MMLOneResolveSync-Setup.exe
;
!include "MUI2.nsh"
!include "FileFunc.nsh"

!define APP_NAME       "MML ONE Resolve Sync"
!define APP_VERSION    "0.1.0"
!define APP_PUBLISHER  "MML ONE"
!define APP_ID         "MMLOneResolveSync"

Name "${APP_NAME}"
OutFile "..\..\dist\windows\MMLOneResolveSync-Setup.exe"
Unicode True
SetCompressor /SOLID lzma

RequestExecutionLevel user
InstallDir "$APPDATA\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Edit"

!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}"

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TEXT "${APP_NAME} is installed.$\r$\n$\r$\nNow open DaVinci Resolve and pick:$\r$\nWorkspace -> Scripts -> Edit -> MMLOneSync"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install" SecInstall
    SetOutPath "$INSTDIR"
    File "..\..\MMLOneSync.py"
    File /r /x __pycache__ "..\..\mml_sync"
    WriteUninstaller "$INSTDIR\uninstall-mmlonesync.exe"
    WriteRegStr   HKCU "${UNINST_KEY}" "DisplayName"     "${APP_NAME}"
    WriteRegStr   HKCU "${UNINST_KEY}" "DisplayVersion"  "${APP_VERSION}"
    WriteRegStr   HKCU "${UNINST_KEY}" "Publisher"       "${APP_PUBLISHER}"
    WriteRegStr   HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegStr   HKCU "${UNINST_KEY}" "UninstallString" "$\"$INSTDIR\uninstall-mmlonesync.exe$\""
    WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
    WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"
SectionEnd

Section "Uninstall"
    Delete "$INSTDIR\MMLOneSync.py"
    RMDir /r "$INSTDIR\mml_sync"
    Delete "$INSTDIR\uninstall-mmlonesync.exe"
    DeleteRegKey HKCU "${UNINST_KEY}"
SectionEnd
