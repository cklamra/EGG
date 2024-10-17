import torch
from collections import OrderedDict, defaultdict
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Union

from rich.live import Live
from rich.columns import Columns
from rich.console import RenderableType
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

import math
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from scipy.stats import pearsonr

from egg.core.callbacks import Callback, CustomProgress
from egg.core.interaction import Interaction


class EpochProgress(Progress):
    class CompletedColumn(ProgressColumn):
        def render(self, task):
            """Calculate common unit for completed and total."""
            download_status = f"{int(task.completed)}/{int(task.total)} ep"
            return Text(download_status, style="progress.download")

    class TransferSpeedColumn(ProgressColumn):
        """Renders human readable transfer speed."""

        def render(self, task):
            """Show data transfer speed."""
            speed = task.speed
            if speed is None:
                return Text("?", style="progress.data.speed")
            speed = f"{1/speed:,.{2}f}"
            return Text(f"{speed} s/ep", style="progress.data.speed")

    def __init__(self, *args, **kwargs):
        super(EpochProgress, self).__init__(*args, **kwargs)


class CustomProgressBarLogger(Callback):
    """
    Displays a progress bar with information about the current epoch and the epoch progression.
    """

    def __init__(
        self,
        n_epochs: int,
        train_data_len: int = 0,
        test_data_len: int = 0,
        print_train_metrics = True,
        step=1,
    ):
        """
        :param n_epochs: total number of epochs
        :param train_data_len: length of the dataset generation for training
        :param test_data_len: length of the dataset generation for testing
        :param use_info_table: true to add an information table on top of the progress bar
        """

        self.n_epochs = n_epochs
        self.train_data_len = train_data_len
        self.test_data_len = test_data_len
        self.print_train_metrics = print_train_metrics
        self.step = step

        self.progress = CustomProgress(
            TextColumn(
                "[bold]{task.fields[cur_epoch]}/{task.fields[n_epochs]} | [blue]{task.fields[mode]}",
                justify="right"),
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>3.1f}%", "•",
            CustomProgress.TransferSpeedColumn(), "•",
            TimeElapsedColumn(), "•",
            TimeRemainingColumn(elapsed_when_finished=True),
            use_info_table=False)
        self.live = Live(self.generate_live_table())
        self.console = self.live.console

        self.live.start()

        self.train_p = self.progress.add_task(
            description="",
            mode="Train",
            cur_epoch=0,
            n_epochs=self.n_epochs,
            start=False,
            visible=False,
            total=self.train_data_len,
        )
        self.test_p = self.progress.add_task(
            description="",
            mode="Eval",
            cur_epoch=0,
            n_epochs=self.n_epochs,
            start=False,
            visible=False,
            total=self.test_data_len,
        )

        self.style = {
            'eval': '',
            'train': 'grey58',
        }

    def build_od(self, logs, loss, epoch, phase):
        od = OrderedDict()
        od["epoch"] = epoch
        od['phase'] = phase
        od["loss"] = loss
        aux = {k: float(torch.mean(v)) if isinstance(v, torch.Tensor) else v for k, v in logs.aux.items()}
        od.update(aux)
        return od
 
    def get_row(self, od, header=False):
        row = Table(expand=True, box=None, show_header=header, show_footer=False)
        for colname in od.keys():
            row.add_column(
                colname,
                justify='left' if colname in ('phase', 'epoch')  else 'right',
                ratio=0.5 if colname in ('phase', 'epoch') else 1)
        if not header:
            row.add_row(
                str(od.pop('epoch')),
                *list(self.format_metric_val(v) for v in od.values()),
                style=self.style[od['phase']])
        return row

    @staticmethod
    def format_metric_val(val):
        if val is None:
            return '–'
        elif isinstance(val, int):
            return str(val)
        elif not isinstance(val, str):
            return f'{val: 4.2f}'
        else:
            return val

    def generate_live_table(self, od=None):
        live_table = Table.grid(expand=True)
        if od:
            header = self.get_row(od=od, header=True)
            live_table.add_row(header)
        live_table.add_row(self.progress)
        return live_table

    def on_epoch_begin(self, epoch: int):
        self.progress.reset(
            task_id=self.train_p,
            total=self.train_data_len,
            start=False,
            visible=False,
            cur_epoch=epoch,
            n_epochs=self.n_epochs,
            mode="Train",
        )
        self.progress.start_task(self.train_p)
        self.progress.update(self.train_p, visible=True)

    def on_batch_end(self, logs: Interaction, loss: float, batch_id: int, is_training: bool = True):
        if is_training:
            self.progress.update(self.train_p, refresh=True, advance=1)
        else:
            self.progress.update(self.test_p, refresh=True, advance=1)

    def on_epoch_end(self, loss: float, logs: Interaction, epoch: int):
        od = self.build_od(logs, loss, epoch, 'train')
        if self.print_train_metrics and (epoch == self.step or self.step == 1):
            self.live.update(self.generate_live_table(od))
        if epoch % self.step == 0:
            row = self.get_row(od)
            self.console.print(row)
        
        self.progress.stop_task(self.train_p)
        self.progress.update(self.train_p, visible=False)

        # if the datalen is zero update with the one epoch just ended
        if self.train_data_len == 0:
            self.train_data_len = self.progress.tasks[self.train_p].completed

        self.progress.reset(
            task_id=self.train_p,
            total=self.train_data_len,
            start=False,
            visible=False,
            cur_epoch=epoch,
            n_epochs=self.n_epochs,
            mode="Train")

    def on_validation_begin(self, epoch: int):
        self.progress.reset(
            task_id=self.test_p,
            total=self.test_data_len,
            start=False,
            visible=False,
            cur_epoch=epoch,
            n_epochs=self.n_epochs,
            mode="Eval")

        self.progress.start_task(self.test_p)
        self.progress.update(self.test_p, visible=True)

    def on_validation_end(self, loss: float, logs: Interaction, epoch: int):
        self.progress.stop_task(self.test_p)
        self.progress.update(self.test_p, visible=False)

        # if the datalen is zero update with the one epoch just ended
        if self.test_data_len == 0:
            self.test_data_len = self.progress.tasks[self.test_p].completed

        self.progress.reset(
            task_id=self.test_p,
            total=self.test_data_len,
            start=False,
            visible=False,
            cur_epoch=epoch,
            n_epochs=self.n_epochs,
            mode="Test")

        od = self.build_od(logs, loss, epoch, 'eval')
        if not self.print_train_metrics and epoch == 1:
            self.live.update(self.generate_live_table(od))
        row = self.get_row(od)
        self.console.print(row)

    def on_train_end(self):
        self.progress.stop()
        self.live.stop()


class LexiconSizeCallback(Callback):
    def __init__(self):
        pass

    def on_validation_end(self, loss: float, logs: Interaction, epoch: int):
        if logs.message is not None:
            lexicon_size = torch.unique(logs.message, dim=0).shape[0]
            logs.aux['lexicon_size'] = int(lexicon_size)

    def on_epoch_end(self, loss: float, logs: Interaction, epoch: int):
        if logs.message is not None:
            if logs.message.dim() == 3:
                message = logs.message.argmax(-1)
            else:
                message = logs.message
            lexicon_size = torch.unique(message, dim=0).shape[0]
            logs.aux['lexicon_size'] = int(lexicon_size)


class AlignmentCallback(Callback):
    def __init__(self, sender, receiver, device, step, bs=32):
        self.sender = sender
        self.receiver = receiver
        self.device = device
        self.step = step
        self.bs = bs

    def on_validation_end(self, loss: float, logs: Interaction, epoch: int):
        object_data = logs.sender_input.to(self.device)
        n_batches = math.ceil(object_data.size()[0]/self.bs)

        sender_embeddings, receiver_embeddings = None, None
        for batch in [object_data[self.bs*y:self.bs*(y+1),:] for y in range(n_batches)]:
            with torch.no_grad():
                b_sender_embeddings = self.sender.fc1(batch).tanh().numpy()
                b_receiver_embeddings = self.receiver.fc1(batch).tanh().numpy()
                if sender_embeddings is None:
                    sender_embeddings = b_sender_embeddings
                    receiver_embeddings = b_receiver_embeddings
                else:
                    sender_embeddings = np.concatenate((sender_embeddings, b_sender_embeddings))
                    receiver_embeddings = np.concatenate((receiver_embeddings, b_receiver_embeddings))

        sender_sims = cosine_similarity(sender_embeddings)
        receiver_sims = cosine_similarity(receiver_embeddings)
        r = pearsonr(sender_sims.ravel(), receiver_sims.ravel())
        logs.aux['alignment'] = r.statistic * 100

    def on_epoch_end(self, loss: float, logs: Interaction, epoch: int):
        logs.aux['alignment'] = None
