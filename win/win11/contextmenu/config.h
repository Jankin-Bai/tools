#pragma once
#include <windows.h>

struct ToolConfig {
    CLSID clsid;
    const wchar_t* title;
    const wchar_t* scriptPath;
    const wchar_t* extraArgs;
};

#define TOOL_COUNT 22
static const CLSID PARENT_CLSID = { 0x8B9F1D2A, 0x3C4E, 0x4F5A, { 0x9B, 0x6D, 0x7E, 0x8F, 0x0A, 0x1B, 0x2C, 0x3D } };
extern const ToolConfig g_toolConfigs[TOOL_COUNT];
