pyinstaller run_voxel.py \
  --name Voxel \
  --windowed \
  --onedir \
  --icon voxel/assets/icon.icns \
  --noconfirm \
  --add-data "voxel/assets/icon.png:assets"

cp -R dist/Voxel.app /Applications/Voxel.app