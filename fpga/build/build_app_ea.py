import vitis, shutil, os
c = vitis.create_client()
c.set_workspace(path=r"D:\vitis_tmp\ws_ea")
a = c.get_component(name="app_ea")
print("BUILD_RESULT", a.build())
src = r"D:\vitis_tmp\ws_ea\app_ea\build\app_ea.elf"
dst = r"D:\vitis_tmp\ws_ea\app_ea\build\app_ea_fpu.elf"
if os.path.isfile(src):
    shutil.copyfile(src, dst)
    print("COPIED", dst, os.path.getsize(dst))
