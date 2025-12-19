from .viewer import DICOMViewer


def main():
    app = DICOMViewer()
    app.mainloop()


if __name__ == "__main__":
    main()
