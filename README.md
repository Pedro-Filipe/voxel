# Voxel

<p align="center">
<img src="voxel/assets/icon_image.png">
</p>

<p align="center">
Voxel - A DICOM Viewer
</p>

A DICOM viewer with advanced features including multi-frame navigation, window/level control, diffusion imaging overlays, freehand ROI analysis, and comprehensive header exploration that supports the modern multi-frame DICOM format.

## 🌟 Features

### Core Viewing Capabilities

- Multi-format DICOM support with automatic file detection
- Multi-frame navigation with frame slider and mouse wheel control
- Interactive zoom and pan with smooth navigation
- Window/Level adjustment via sliders or right-click drag
- Crosshair overlay with real-time pixel value readout

### Advanced Analysis Tools

- Freehand ROI drawing with comprehensive statistics (mean, std, median, IQR)
- Diffusion imaging overlays showing b-values.
- Hierarchical series organization by Study → Series → Instance
- Real-time pixel value inspection (stored values, modality LUT)

### Data Organization & Navigation

- Hierarchical file tree with Study/Series/Instance organization
- Smart filtering for both file lists and DICOM headers
- Least Recently Used (LRU) caching for improved performance with large datasets
- Keyboard shortcuts for efficient navigation

### Header Exploration

- Multi-scope header viewing: Dataset, Shared Functional Groups, Per-frame
- Hierarchical tree structure with expandable sequences
- Real-time filtering with search highlighting
- Frame-linked updates for enhanced functional group analysis

## 📦 Installation

Prerequisites

**You need a version of Python with Tkinter installed.**

```bash
pip install -r requirements.txt
```

More details on troubleshooting Tkinter installation issues can be found in the [Tkinter Issues and Fixes](docs/tkinter_issues.md) document.

## Run the application

```bash
python -m voxel.main
```

## 🤝 Contributing

- Fork the repository
- Create a feature branch (git checkout -b feature/amazing-feature)
- Commit your changes (git commit -m 'Add amazing feature')
- Push to the branch (git push origin feature/amazing-feature)
- Open a Pull Request

## 📝 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 👨‍💻 Author

Pedro Ferreira - Initial work and development
