from PyQt5 import QtCore, QtWidgets


class Ui_MainWindow(object):
    def setupUi(self, MainWindow):
        MainWindow.setObjectName("MainWindow")
        MainWindow.resize(560, 360)
        MainWindow.setMinimumSize(QtCore.QSize(560, 360))

        self.centralwidget = QtWidgets.QWidget(MainWindow)
        self.centralwidget.setObjectName("centralwidget")
        MainWindow.setCentralWidget(self.centralwidget)

        self.root = QtWidgets.QVBoxLayout(self.centralwidget)
        self.root.setContentsMargins(10, 10, 10, 10)
        self.root.setSpacing(8)

        # title
        self.label = QtWidgets.QLabel(self.centralwidget)
        self.label.setObjectName("label")
        self.label.setAlignment(QtCore.Qt.AlignCenter)
        self.label.setStyleSheet('font: 26pt "MS Serif";')
        self.root.addWidget(self.label)

        # log area: Oracle và API tách 2 tab độc lập
        self.log_tabs = QtWidgets.QTabWidget(self.centralwidget)
        self.log_tabs.setObjectName("log_tabs")

        self.tab_oracle = QtWidgets.QWidget()
        self.tab_oracle.setObjectName("tab_oracle")
        self.tab_oracle_layout = QtWidgets.QVBoxLayout(self.tab_oracle)
        self.tab_oracle_layout.setContentsMargins(0, 0, 0, 0)
        self.tab_oracle_layout.setSpacing(0)
        self.listWidget = QtWidgets.QListWidget(self.tab_oracle)
        self.listWidget.setObjectName("listWidget")
        self.tab_oracle_layout.addWidget(self.listWidget)
        self.log_tabs.addTab(self.tab_oracle, "")

        self.tab_api = QtWidgets.QWidget()
        self.tab_api.setObjectName("tab_api")
        self.tab_api_layout = QtWidgets.QVBoxLayout(self.tab_api)
        self.tab_api_layout.setContentsMargins(0, 0, 0, 0)
        self.tab_api_layout.setSpacing(0)
        self.listWidget_api = QtWidgets.QListWidget(self.tab_api)
        self.listWidget_api.setObjectName("listWidget_api")
        self.tab_api_layout.addWidget(self.listWidget_api)
        self.log_tabs.addTab(self.tab_api, "")

        self.tab_sql = QtWidgets.QWidget()
        self.tab_sql.setObjectName("tab_sql")
        self.tab_sql_layout = QtWidgets.QVBoxLayout(self.tab_sql)
        self.tab_sql_layout.setContentsMargins(0, 0, 0, 0)
        self.tab_sql_layout.setSpacing(0)
        self.listWidget_sql = QtWidgets.QListWidget(self.tab_sql)
        self.listWidget_sql.setObjectName("listWidget_sql")
        self.tab_sql_layout.addWidget(self.listWidget_sql)
        self.log_tabs.addTab(self.tab_sql, "")

        self.root.addWidget(self.log_tabs, 1)

        # bottom row
        self.bottom_row = QtWidgets.QHBoxLayout()
        self.bottom_row.setContentsMargins(0, 0, 0, 0)
        self.bottom_row.setSpacing(8)

        self.btn_manual_reset = QtWidgets.QPushButton(self.centralwidget)
        self.btn_manual_reset.setObjectName("btn_manual_reset")
        self.btn_manual_reset.setMinimumSize(QtCore.QSize(24, 24))
        self.btn_manual_reset.setMaximumSize(QtCore.QSize(24, 24))
        self.btn_manual_reset.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_manual_reset.setStyleSheet(
            """
            QPushButton#btn_manual_reset {
                background-color: #1f5aa6;
                color: white;
                border: 1px solid #17457f;
                border-radius: 12px;
                font: bold 10pt "Arial";
                padding: 0px;
            }
            QPushButton#btn_manual_reset:hover {
                background-color: #2b6fc7;
                border: 1px solid #1f5aa6;
            }
            QPushButton#btn_manual_reset:pressed {
                background-color: #173f75;
                border: 1px solid #12325d;
            }
            """
        )
        self.bottom_row.addWidget(self.btn_manual_reset)

        self.label_next_run_caption = QtWidgets.QLabel(self.centralwidget)
        self.label_next_run_caption.setObjectName("label_next_run_caption")
        self.bottom_row.addWidget(self.label_next_run_caption)

        self.label_next_run_value = QtWidgets.QLabel(self.centralwidget)
        self.label_next_run_value.setObjectName("label_next_run_value")
        self.label_next_run_value.setMinimumWidth(160)
        self.bottom_row.addWidget(self.label_next_run_value)

        self.bottom_row.addStretch(1)

        self.label_table1_caption = QtWidgets.QLabel(self.centralwidget)
        self.label_table1_caption.setObjectName("label_table1_caption")
        self.bottom_row.addWidget(self.label_table1_caption)

        self.label_table1_value = QtWidgets.QLabel(self.centralwidget)
        self.label_table1_value.setObjectName("label_table1_value")
        self.label_table1_value.setMinimumWidth(45)
        self.bottom_row.addWidget(self.label_table1_value)

        self.label_table2_caption = QtWidgets.QLabel(self.centralwidget)
        self.label_table2_caption.setObjectName("label_table2_caption")
        self.bottom_row.addWidget(self.label_table2_caption)

        self.label_table2_value = QtWidgets.QLabel(self.centralwidget)
        self.label_table2_value.setObjectName("label_table2_value")
        self.label_table2_value.setMinimumWidth(45)
        self.bottom_row.addWidget(self.label_table2_value)

        self.label_table3_caption = QtWidgets.QLabel(self.centralwidget)
        self.label_table3_caption.setObjectName("label_table3_caption")
        self.bottom_row.addWidget(self.label_table3_caption)

        self.label_table3_value = QtWidgets.QLabel(self.centralwidget)
        self.label_table3_value.setObjectName("label_table3_value")
        self.label_table3_value.setMinimumWidth(55)
        self.bottom_row.addWidget(self.label_table3_value)

        self.root.addLayout(self.bottom_row)

        # ===== Overlay nằm hoàn toàn trong Gui_main.py =====
        self.password_overlay = QtWidgets.QWidget(self.centralwidget)
        self.password_overlay.setObjectName("password_overlay")
        self.password_overlay.setStyleSheet(
            """
            QWidget#password_overlay {
                background-color: rgba(0, 0, 0, 70);
            }
            QFrame#password_panel {
                background-color: rgb(245, 245, 245);
                border: 1px solid #808080;
                border-radius: 8px;
            }
            QLabel#label_password_title {
                font: 10pt "Arial";
                color: black;
            }
            QLineEdit#password_edit {
                border: 1px solid #9a9a9a;
                border-radius: 4px;
                padding: 5px 8px;
                background: white;
                min-height: 28px;
            }
            QPushButton#btn_password_ok {
                background-color: #1f7a3a;
                color: white;
                border: 1px solid #16602d;
                border-radius: 4px;
                padding: 4px 12px;
                min-width: 70px;
                min-height: 28px;
            }
            QPushButton#btn_password_ok:hover {
                background-color: #249145;
            }
            QPushButton#btn_password_ok:pressed {
                background-color: #15562a;
            }
            QPushButton#btn_password_cancel {
                background-color: #d9d9d9;
                color: black;
                border: 1px solid #a0a0a0;
                border-radius: 4px;
                padding: 4px 12px;
                min-width: 70px;
                min-height: 28px;
            }
            QPushButton#btn_password_cancel:hover {
                background-color: #cfcfcf;
            }
            QPushButton#btn_password_cancel:pressed {
                background-color: #bdbdbd;
            }
            """
        )
        self.password_overlay.hide()

        # overlay full-size layout
        self.password_overlay_layout = QtWidgets.QVBoxLayout(self.password_overlay)
        self.password_overlay_layout.setContentsMargins(0, 0, 0, 0)
        self.password_overlay_layout.setSpacing(0)

        self.password_overlay_layout.addStretch(1)

        self.password_row = QtWidgets.QHBoxLayout()
        self.password_row.setContentsMargins(0, 0, 0, 0)
        self.password_row.setSpacing(0)
        self.password_row.addStretch(1)

        self.password_panel = QtWidgets.QFrame(self.password_overlay)
        self.password_panel.setObjectName("password_panel")
        self.password_panel.setFixedSize(270, 140)

        self.password_panel_layout = QtWidgets.QVBoxLayout(self.password_panel)
        self.password_panel_layout.setContentsMargins(14, 14, 14, 14)
        self.password_panel_layout.setSpacing(10)

        self.label_password_title = QtWidgets.QLabel(self.password_panel)
        self.label_password_title.setObjectName("label_password_title")
        self.label_password_title.setAlignment(QtCore.Qt.AlignCenter)
        self.password_panel_layout.addWidget(self.label_password_title)

        self.password_edit = QtWidgets.QLineEdit(self.password_panel)
        self.password_edit.setObjectName("password_edit")
        self.password_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.password_panel_layout.addWidget(self.password_edit)

        self.password_button_row = QtWidgets.QHBoxLayout()
        self.password_button_row.setContentsMargins(0, 0, 0, 0)
        self.password_button_row.setSpacing(8)

        self.btn_password_ok = QtWidgets.QPushButton(self.password_panel)
        self.btn_password_ok.setObjectName("btn_password_ok")
        self.password_button_row.addWidget(self.btn_password_ok)

        self.btn_password_cancel = QtWidgets.QPushButton(self.password_panel)
        self.btn_password_cancel.setObjectName("btn_password_cancel")
        self.password_button_row.addWidget(self.btn_password_cancel)

        self.password_panel_layout.addLayout(self.password_button_row)

        self.password_row.addWidget(self.password_panel)
        self.password_row.addStretch(1)

        self.password_overlay_layout.addLayout(self.password_row)
        self.password_overlay_layout.addStretch(1)

        self.retranslateUi(MainWindow)
        QtCore.QMetaObject.connectSlotsByName(MainWindow)

        self._bind_resize(MainWindow)
        self._sync_overlay_geometry()

    def _sync_overlay_geometry(self):
        self.password_overlay.setGeometry(self.centralwidget.rect())
        self.password_overlay.raise_()

    def _bind_resize(self, MainWindow):
        old_resize_event = MainWindow.resizeEvent

        def new_resize_event(event):
            if old_resize_event:
                old_resize_event(event)
            self._sync_overlay_geometry()

        MainWindow.resizeEvent = new_resize_event

    def retranslateUi(self, MainWindow):
        _tr = QtCore.QCoreApplication.translate
        MainWindow.setWindowTitle(_tr("MainWindow", "PLC READER -> Oracle + API + SQL"))
        self.label.setText(_tr("MainWindow", "MACHINE STATUS PROGRAM"))
        self.log_tabs.setTabText(self.log_tabs.indexOf(self.tab_oracle), _tr("MainWindow", "ORACLE LOG"))
        self.log_tabs.setTabText(self.log_tabs.indexOf(self.tab_api), _tr("MainWindow", "API LOG"))
        self.log_tabs.setTabText(self.log_tabs.indexOf(self.tab_sql), _tr("MainWindow", "SQL LOG"))

        self.btn_manual_reset.setText(_tr("MainWindow", "R"))
        self.btn_manual_reset.setToolTip(_tr("MainWindow", "Manual reset PASS/FAIL"))

        self.label_next_run_caption.setText(_tr("MainWindow", "Next run :"))
        self.label_next_run_value.setText(_tr("MainWindow", "-"))

        self.label_table1_caption.setText(_tr("MainWindow", "Table1:"))
        self.label_table1_value.setText(_tr("MainWindow", "-"))

        self.label_table2_caption.setText(_tr("MainWindow", "Table2:"))
        self.label_table2_value.setText(_tr("MainWindow", "-"))

        self.label_table3_caption.setText(_tr("MainWindow", "Table3:"))
        self.label_table3_value.setText(_tr("MainWindow", "-"))

        self.label_password_title.setText(_tr("MainWindow", "Nhập mật khẩu reset"))
        self.btn_password_ok.setText(_tr("MainWindow", "OK"))
        self.btn_password_cancel.setText(_tr("MainWindow", "Cancel"))


if __name__ == "__main__":
    import sys

    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)

    app = QtWidgets.QApplication(sys.argv)
    w = QtWidgets.QMainWindow()
    ui = Ui_MainWindow()
    ui.setupUi(w)
    w.show()
    sys.exit(app.exec_())
