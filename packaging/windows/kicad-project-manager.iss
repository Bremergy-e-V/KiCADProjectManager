; Inno Setup script for the Windows installer.
;
; Every path and version comes from the workflow via /D:
;
;   ISCC /DAppVersion=0.1.0-build.7 /DFileVersion=0.1.0.7 ^
;        /DSourceDir=...\dist\KiCadProjectManager /DIconFile=...\build\icon.ico ^
;        /DOutputDir=...\out /DOutputName=... kicad-project-manager.iss

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef FileVersion
  #define FileVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\KiCadProjectManager"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\out"
#endif
#ifndef OutputName
  #define OutputName "KiCadProjectManager-setup"
#endif
#ifndef IconFile
  #define IconFile "..\..\build\icon.ico"
#endif

#define AppName "KiCad Project Manager"
#define AppPublisher "Bremergy e.V."
#define AppExe "KiCadProjectManager.exe"

[Setup]
; Never reuse this GUID for another product - it is what lets an installer
; recognise and upgrade a previous install in place.
AppId={{92FDF988-2154-4A01-8A5A-933CDE6B1D1A}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppSupportURL=https://github.com/Bremergy-e-V/KiCADProjectManager
VersionInfoVersion={#FileVersion}
; Per-user install: no admin rights needed, and the in-app updater can run the
; next installer silently without a UAC prompt.
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile={#IconFile}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Dependencies change between builds; clear the old ones so nothing stale is left behind.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; The in-app updater runs this installer silently, which skips the launch above.
; It asks for a restart explicitly with /RELAUNCH=1, which nothing else passes.
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: RelaunchRequested

[Code]
function RelaunchRequested(): Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

function GitInstalled(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/C git --version', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (not WizardSilent()) and (not GitInstalled()) then
    MsgBox('{#AppName} needs Git to download and upload projects.' + #13#10 + #13#10 +
           'Please install it from https://git-scm.com/downloads/win before starting the app.',
           mbInformation, MB_OK);
end;
