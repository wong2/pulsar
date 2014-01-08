#ifndef _PULSAR_LUA_EXTRA_H_
#define _PULSAR_LUA_EXTRA_H_

#include <stdlib.h>
#include <math.h>

#include "lua.h"
#include "lualib.h"
#include "lauxlib.h"

#include "Python.h"


#ifdef _MSC_VER
#define STIN static __inline
#define EXIN extern __inline
#define snprintf _snprintf
#define isnan(x) _isnan(x)
#define isinf(x) (!_finite(x))
#define strncasecmp _strnicmp
#else
#define STIN static inline
#define EXIN extern inline
#endif

#define CJSON_MODNAME   "cjson"
LUAMOD_API int (luaopen_cjson) (lua_State *L);


LUALIB_API PyObject* all_libs(lua_State *L);
LUALIB_API int (load_lib) (lua_State *L, const char* name);

#endif  //  _PULSAR_LUA_EXTRA_H_