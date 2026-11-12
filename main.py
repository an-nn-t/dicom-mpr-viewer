"""DICOM 3 断面ビューア（知能情報実験実習２ 医用画像処理実験 最終課題）

起動: python main.py
"""
import os
import sys

import numpy as np
import pydicom
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PIL import Image, ImageSequence
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QPushButton, QSlider, QVBoxLayout, QWidget)
from scipy import ndimage

import vtkmodules.qt
vtkmodules.qt.PyQtImpl = "PySide6"  # VTK の画面部品にも PySide6 を使わせる
import vtk  # noqa: E402
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor  # noqa: E402
from vtkmodules.util.numpy_support import numpy_to_vtk  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LUNG_TIFF = os.path.join(DATA_DIR, "lung_mask_session5.tif")  # 第 5 回で抽出した肺領域

# CT は (z, y, x) の 3 次元配列．各断面の (名前, 色, スライス方向の軸)
VIEWS = [("Axial", "red", 0), ("Coronal", "lime", 1), ("Sagittal", "yellow", 2)]


def load_dicom_folder(folder):
    """フォルダ内の DICOM を読み，CT 値 (z, y, x) と画素間隔 (dz, dy, dx) [mm] を返す"""
    paths = [os.path.join(root, name) for root, _, names in os.walk(folder)
             for name in names if name.lower().endswith(".dcm")]
    if len(paths) < 2:
        raise ValueError("DICOM ファイル（.dcm）が 2 枚以上入ったフォルダを選んでください．")
    slices = [pydicom.dcmread(p) for p in paths]
    slices.sort(key=lambda ds: float(ds.ImagePositionPatient[2]))  # スライス位置順（足 → 頭）
    # 画素値 → CT 値: HU = 画素値 × RescaleSlope + RescaleIntercept
    hu = np.stack([(ds.pixel_array * float(ds.RescaleSlope) + float(ds.RescaleIntercept))
                   .astype(np.int16) for ds in slices])
    dy, dx = (float(v) for v in slices[0].PixelSpacing)
    dz = abs(float(slices[1].ImagePositionPatient[2]) - float(slices[0].ImagePositionPatient[2]))
    return hu, (dz, dy, dx)


def load_lung_mask(path):
    """第 5 回の肺領域（マルチページ TIFF，肺 = 255）を読み，寝台などの小さい領域を除く"""
    with Image.open(path) as im:
        mask = np.stack([np.array(page) for page in ImageSequence.Iterator(im)]) > 0
    # 肺とつながっていない小さい領域（寝台のすき間・腸のガスなど）を除く
    labels, _ = ndimage.label(mask)
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    return (sizes >= sizes.max() * 0.1)[labels]


class SliceView(QWidget):
    """1 つの断面: 画像，他の 2 断面の位置を示す線，スライダー（スライス・WL・WW）"""

    def __init__(self, viewer, index):
        super().__init__()
        self.viewer = viewer
        name, color, self.axis = VIEWS[index]
        # 画像の縦・横がボリュームのどの軸か（Axial: y, x / Coronal: z, x / Sagittal: z, y）
        self.row_axis, self.col_axis = [a for a in range(3) if a != self.axis]

        title = QLabel(name)
        title.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 14px;")
        self.fig = Figure(facecolor="black")
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.ax = self.fig.add_axes((0, 0, 1, 1))
        self.ax.set_axis_off()

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self.canvas, 1)
        self.slice_slider, self.slice_label = self._add_slider(layout, "スライス", 0, 0, 0)
        self.wl_slider, self.wl_label = self._add_slider(layout, "WL", -1000, 1000, -600)
        self.ww_slider, self.ww_label = self._add_slider(layout, "WW", 1, 2000, 1500)
        # スライスを変えたら 3 断面すべてを描き直す（他の断面の線が動く: 観点 6）
        self.slice_slider.valueChanged.connect(self.on_slice_changed)
        self.wl_slider.valueChanged.connect(self.update_view)
        self.ww_slider.valueChanged.connect(self.update_view)

    def _add_slider(self, layout, name, minimum, maximum, value):
        row = QHBoxLayout()
        label = QLabel(name)
        label.setFixedWidth(50)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        slider.setEnabled(False)  # DICOM を開くまでは操作できない
        value_label = QLabel(str(value))
        value_label.setFixedWidth(70)
        row.addWidget(label)
        row.addWidget(slider, 1)
        row.addWidget(value_label)
        layout.addLayout(row)
        return slider, value_label

    def set_volume(self, hu, spacing):
        """新しい CT を表示する準備をする"""
        self.ax.clear()
        self.ax.set_axis_off()
        n = hu.shape[self.axis]
        self.slice_slider.blockSignals(True)
        self.slice_slider.setRange(0, n - 1)
        self.slice_slider.setValue(n // 2)
        self.slice_slider.blockSignals(False)
        for slider in (self.slice_slider, self.wl_slider, self.ww_slider):
            slider.setEnabled(True)
        # Axial は上が前，Coronal・Sagittal は上が頭になるように表示する
        # aspect: 縦と横の画素間隔の比（スライス間隔と画素間隔の違いを補正）
        self.image = self.ax.imshow(
            self.current_image(), cmap="gray", origin="upper" if self.axis == 0 else "lower",
            aspect=spacing[self.row_axis] / spacing[self.col_axis])
        # 他の 2 断面の位置を示す線（観点 4）．線の色はその断面の色
        self.hline = self.ax.axhline(0, color=VIEWS[self.row_axis][1], lw=1)
        self.vline = self.ax.axvline(0, color=VIEWS[self.col_axis][1], lw=1)

    def current_image(self):
        return np.take(self.viewer.hu, self.viewer.position[self.axis], axis=self.axis)

    def on_slice_changed(self, value):
        self.viewer.position[self.axis] = value
        self.viewer.update_all()

    def update_view(self):
        if self.viewer.hu is None:
            return
        wl, ww = self.wl_slider.value(), self.ww_slider.value()
        self.image.set_data(self.current_image())
        self.image.set_clim(wl - ww / 2, wl + ww / 2)
        pos = self.viewer.position
        self.hline.set_ydata([pos[self.row_axis]] * 2)
        self.vline.set_xdata([pos[self.col_axis]] * 2)
        self.slice_label.setText(f"{pos[self.axis] + 1} / {self.slice_slider.maximum() + 1}")
        self.wl_label.setText(str(wl))
        self.ww_label.setText(str(ww))
        self.canvas.draw_idle()


class LungWindow(QWidget):
    """肺領域のボリュームレンダリング（観点 7）．左ドラッグで回転，右ドラッグ・ホイールで拡大縮小"""

    def __init__(self, mask, spacing):
        super().__init__()
        self.setWindowTitle("肺領域の 3D 表示")
        self.resize(800, 800)

        # 肺の周りだけ切り出し，少しぼかして表面をなめらかにする（0〜255 の画像にする）
        z, y, x = ndimage.find_objects(mask.astype(np.uint8))[0]
        volume = np.pad(mask[z, y, x], 3).astype(np.float32)
        volume = (ndimage.gaussian_filter(volume, 1.0) * 255).astype(np.uint8)

        image = vtk.vtkImageData()
        image.SetDimensions(volume.shape[2], volume.shape[1], volume.shape[0])  # (x, y, z)
        dz, dy, dx = spacing
        image.SetSpacing(dx, dy, dz)
        image.GetPointData().SetScalars(numpy_to_vtk(volume.ravel(), deep=True))

        color = vtk.vtkColorTransferFunction()
        color.AddRGBPoint(0, 0.9, 0.5, 0.45)
        color.AddRGBPoint(255, 1.0, 0.7, 0.65)
        opacity = vtk.vtkPiecewiseFunction()  # 肺の境界（値 128 付近）から不透明にする
        opacity.AddPoint(0, 0.0)
        opacity.AddPoint(100, 0.0)
        opacity.AddPoint(160, 0.8)
        opacity.AddPoint(255, 0.8)
        prop = vtk.vtkVolumeProperty()
        prop.SetColor(color)
        prop.SetScalarOpacity(opacity)
        prop.SetInterpolationTypeToLinear()
        prop.ShadeOn()

        mapper = vtk.vtkSmartVolumeMapper()
        mapper.SetInputData(image)
        actor = vtk.vtkVolume()
        actor.SetMapper(mapper)
        actor.SetProperty(prop)
        renderer = vtk.vtkRenderer()
        renderer.AddVolume(actor)
        renderer.SetBackground(0.1, 0.1, 0.15)
        # 正面（患者の前）から見る．画面の上が頭側
        camera = renderer.GetActiveCamera()
        camera.SetPosition(0, -1, 0)
        camera.SetFocalPoint(0, 0, 0)
        camera.SetViewUp(0, 0, 1)
        renderer.ResetCamera()

        self.vtk_widget = QVTKRenderWindowInteractor(self)
        self.vtk_widget.GetRenderWindow().AddRenderer(renderer)
        self.vtk_widget.GetRenderWindow().GetInteractor().SetInteractorStyle(
            vtk.vtkInteractorStyleTrackballCamera())
        QVBoxLayout(self).addWidget(self.vtk_widget)
        self.vtk_widget.Initialize()

    def closeEvent(self, event):
        self.vtk_widget.Finalize()  # VTK の後片付け（終了時に落ちるのを防ぐ）
        super().closeEvent(event)


class Viewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DICOM 3 断面ビューア")
        self.resize(1500, 650)
        self.hu = None
        self.spacing = None
        self.position = [0, 0, 0]  # 3 断面の位置 (z, y, x)
        self.lung_window = None

        # メニュー（観点 2: ファイル，観点 7: 3D 表示）
        for menu_name, items in [("ファイル", [("DICOM フォルダを開く...", self.open_dicom),
                                              ("終了", self.close)]),
                                 ("3D 表示", [("肺領域（第 5 回）を 3D 表示", self.open_lung)])]:
            menu = self.menuBar().addMenu(menu_name)
            for text, slot in items:
                action = QAction(text, self)
                action.triggered.connect(slot)
                menu.addAction(action)

        # 画面上部のボタン（使う順に番号を付ける）
        buttons = QHBoxLayout()
        for text, slot in [("① DICOM フォルダを開く", self.open_dicom),
                           ("② 肺領域（第 5 回）を 3D 表示", self.open_lung)]:
            button = QPushButton(text)
            button.setStyleSheet("font-size: 14px; padding: 6px 16px;")
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)

        # Axial・Coronal・Sagittal を横に並べる（観点 1・3）
        self.views = [SliceView(self, i) for i in range(3)]
        row = QHBoxLayout()
        for view in self.views:
            row.addWidget(view)
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(buttons)
        layout.addLayout(row, 1)
        self.setCentralWidget(central)

    def open_dicom(self):
        folder = QFileDialog.getExistingDirectory(self, "DICOM ファイルが入ったフォルダを選択")
        if not folder:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.hu, self.spacing = load_dicom_folder(folder)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "エラー", f"DICOM を読み込めませんでした．\n{exc}")
            return
        QApplication.restoreOverrideCursor()
        self.position = [n // 2 for n in self.hu.shape]
        for view in self.views:
            view.set_volume(self.hu, self.spacing)
        self.update_all()
        self.statusBar().showMessage(
            f"{folder}（{self.hu.shape[0]} 枚）を読み込みました．「② 肺領域を 3D 表示」で 3D 表示できます．")

    def update_all(self):
        for view in self.views:
            view.update_view()

    def open_lung(self):
        """第 5 回で抽出した肺領域（data/lung_mask_session5.tif）を 3D 表示する（観点 7）"""
        if self.hu is None:
            QMessageBox.information(self, "3D 表示",
                                    "先に「① DICOM フォルダを開く」で LIDC-IDRI-0265 の CT を開いてください．")
            return
        path = LUNG_TIFF
        if not os.path.exists(path):  # 同梱の TIFF がないときだけ選んでもらう
            path, _ = QFileDialog.getOpenFileName(self, "第 5 回の肺領域（TIFF）を選択", DATA_DIR,
                                                  "TIFF (*.tif *.tiff)")
            if not path:
                return
        self.close_lung_window()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            mask = load_lung_mask(path)
            if mask.shape != self.hu.shape:
                raise ValueError(f"肺領域の大きさ {mask.shape} が CT {self.hu.shape} と違います．")
            self.lung_window = LungWindow(mask, self.spacing)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "エラー", f"3D 表示できませんでした．\n{exc}")
            return
        QApplication.restoreOverrideCursor()
        self.lung_window.show()

    def close_lung_window(self):
        if self.lung_window is not None and self.lung_window.isVisible():
            self.lung_window.close()

    def closeEvent(self, event):
        self.close_lung_window()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    viewer = Viewer()
    viewer.show()
    sys.exit(app.exec())
