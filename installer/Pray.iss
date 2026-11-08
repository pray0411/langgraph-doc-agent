; Pray Windows 安装脚本（Inno Setup 6）
;
; 为什么用 lowest 权限 + {localappdata}（而不是 Program Files）：
;   1) 免管理员权限：用户双击 setup 就能装，没有 UAC 摩擦 —— 分发场景下这是最
;      大的一道心理门槛；
;   2) 安装目录必须**可写**：程序会在 exe 同级读写 .env / data / generated /
;      logs（配置持久化依赖这一点）。装进 Program Files 后普通用户不可写，
;      "设置面板保存 API Key"会失败。
;
; 构建：
;   ISCC.exe Pray.iss /DAppVersion=1.1.0
; 产物：dist\PraySetup-<版本>-win64.exe
;
; 说明：安装包本身**不含 .env**（密钥不入镜像/安装包）；用户首次启动时
; 由界面上的引导填写，程序保存到 {app}\.env。

#define AppName "Pray"
#define AppPublisher "pray0411"
#define AppURL "https://github.com/pray0411/langgraph-doc-agent"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8E6B2C4A-1F3D-4B7A-9C2E-7A5D3B1F0C9E}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
; 装到用户目录，无需管理员权限（见文件顶部说明）
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 只允许 64 位 Windows（PyInstaller 产物为 x64）
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
LicenseFile=
OutputDir=..\dist
OutputBaseFilename=PraySetup-{#AppVersion}-win64
SetupIconFile=..\assets\icon-ui.ico
UninstallDisplayIcon={app}\Pray.exe
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 安装/升级前提示关闭正在运行的 Pray（exe 被占用会导致替换失败）
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; 主程序与全部运行时依赖（PyInstaller onedir 产物）
Source: "..\dist\Pray\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 配置模板：用户可参考，但真实 .env 由程序在首次配置时生成
Source: "..\.env.example"; DestDir: "{app}"; DestName: ".env.example"; Flags: ignoreversion
Source: "..\使用说明.txt"; DestDir: "{app}"; DestName: "使用说明.txt"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\Pray.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Pray.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Pray.exe"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清理可再生的运行时产物；用户数据目录（generated / data）保留，
; 避免卸载顺手删掉用户生成的文件与会话记忆。
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\index"
Type: files; Name: "{app}\.env.tmp"
