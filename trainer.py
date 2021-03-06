from tqdm import tqdm
from utils import AverageMeter
import logging

logger = logging.getLogger('RAM')


class Trainer(object):
    """
    Trainer encapsulates all the logic necessary for training.
    """
    def __init__(self, model, optimizer, watch=[], val_watch=[]):
        self.model = model
        self.optimizer = optimizer
        self.stop_training = False
        self.watch = watch
        self.val_watch = val_watch
        if 'loss' not in watch:
            watch.insert(0, 'loss')
        if 'loss' not in val_watch:
            val_watch.insert(0, 'loss')

    def train(self, train_loader, val_loader, start_epoch=0, epochs=200, callbacks=[]):
        for epoch in range(start_epoch, epochs):
            if self.stop_training:
                return
            epoch_log = self.train_one_epoch(epoch, train_loader, callbacks=callbacks)
            val_log = self.validate(epoch, val_loader)

            msg = ' '.join(['{}: {:.3f}'.format(name, avg) for name, avg in epoch_log.items()])
            logger.info(msg)
            msg = ' '.join(['{}: {:.3f}'.format(name, avg) for name, avg in val_log.items()])
            logger.info(msg)
            epoch_log.update(val_log)

            for cbk in callbacks:
                cbk.on_epoch_end(epoch, epoch_log)

    def train_one_epoch(self, epoch, train_loader, callbacks=[]):
        """
        Train the model for 1 epoch of the training set.
        """
        epoch_log = {name: AverageMeter() for name in self.watch}

        for i, (x, y) in enumerate(tqdm(train_loader, unit='batch', desc='Epoch {:>3}'.format(epoch))):
            metric = self.model.forward(x, y, is_training=True)
            loss = metric['loss']
            for name, avg in epoch_log.items():
                epoch_log[name].update(metric[name].data[0], x.size()[0])

            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            for cbk in callbacks:
                cbk.on_batch_end(epoch, i, logs=metric)

        return {name: meter.avg for name, meter in epoch_log.items()}

    def validate(self, epoch, val_loader):
        """
        Evaluate the model on the validation set.
        """
        val_log = {name: AverageMeter() for name in self.watch}

        for i, (x, y) in enumerate(val_loader):
            # metric = self.model.forward(x, y, is_training=False)
            metric = self.model.forward(x, y)
            for name, avg in val_log.items():
                val_log[name].update(metric[name].data[0], x.size()[0])

        return {'val_'+name: meter.avg for name, meter in val_log.items()}

    def test(self, test_loader, best=True):
        """
        Test the model on the held-out test data.
        This function should only be called at the very
        end once the model has finished training.
        """
        # load the best checkpoint
        self.load_checkpoint(best=best)

        accs = AverageMeter()

        for i, (x, y) in enumerate(test_loader):
            metric = self.model.forward(x, y)
            acc = metric['acc']

            accs.update(acc.data[0], x.size()[0])

        logger.info('Test Acc: {}/{} ({:.2f}%)'.format(accs.sum, accs.n, accs.avg))
