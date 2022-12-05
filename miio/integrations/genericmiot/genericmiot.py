import logging
from functools import partial
from typing import Optional

from miio import MiotDevice
from miio.click_common import command
from miio.descriptors import AccessFlags, ActionDescriptor, PropertyDescriptor
from miio.miot_cloud import MiotCloud
from miio.miot_device import MiotMapping
from miio.miot_models import DeviceModel, MiotAccess, MiotAction, MiotService

from .status import GenericMiotStatus

from .meta import Metadata

_LOGGER = logging.getLogger(__name__)


def pretty_status(result: "GenericMiotStatus", verbose=False):
    """Pretty print status information."""
    out = ""
    props = result.property_dict()
    for _name, prop in props.items():
        pretty_value = prop.pretty_value

        if "write" in prop.access:
            out += "[S] "

        out += f"{prop.description} ({prop.name}): {pretty_value}"

        if prop.choices is not None:  # TODO: hide behind verbose flag?
            out += (
                " (from: "
                + ", ".join([f"{c.description} ({c.value})" for c in prop.choices])
                + ")"
            )

        if prop.range is not None:  # TODO: hide behind verbose flag?
            out += (
                f" (min: {prop.range[0]}, max: {prop.range[1]}, step: {prop.range[2]})"
            )

        if verbose:
            out += f" ({prop.full_name})"

        out += "\n"

    return out


def pretty_actions(result: Dict[str, ActionDescriptor]):
    """Pretty print actions."""
    out = ""
    for _, desc in result.items():
        out += f"{desc.id}\t\t{desc.name}\n"

    return out


def pretty_settings(result: Dict[str, SettingDescriptor]):
    """Pretty print settings."""
    out = ""
    for _, desc in result.items():
        out += f"# {desc.id} ({desc.name})"
        out += f"  urn: {repr(desc.extras['urn'])}\n"
        out += f"  siid: {desc.extras['siid']}\n"
        out += f"  piid: {desc.extras['piid']}\n"

    return out


class GenericMiotStatus(DeviceStatus):
    """Generic status for miot devices."""

    def __init__(self, response, dev):
        self._model: DeviceModel = dev._miot_model
        self._dev = dev
        self._data = {elem["did"]: elem["value"] for elem in response}
        self._data_by_siid_piid = {
            (elem["siid"], elem["piid"]): elem["value"] for elem in response
        }

    def __getattr__(self, item):
        """Return attribute for name.

        This is overridden to provide access to properties using (siid, piid) tuple.
        """
        # TODO: find a better way to encode the property information
        serv, prop = item.split(":")
        prop = self._model.get_property(serv, prop)
        value = self._data[item]

        # TODO: this feels like a wrong place to convert value to enum..
        if prop.choices is not None:
            for choice in prop.choices:
                if choice.value == value:
                    return choice.description

            _LOGGER.warning(
                "Unable to find choice for value: %s: %s", value, prop.choices
            )

        return self._data[item]

    def property_dict(self) -> Dict[str, MiotProperty]:
        """Return name-keyed dictionary of properties."""
        res = {}

        # We use (siid, piid) to locate the property as not all devices mirror the did in response
        for (siid, piid), value in self._data_by_siid_piid.items():
            prop = self._model.get_property_by_siid_piid(siid, piid)
            prop.value = value
            res[prop.name] = prop

        return res

    def __repr__(self):
        s = f"<{self.__class__.__name__}"
        for name, value in self.property_dict().items():
            s += f" {name}={value}"
        s += ">"

        return s


class GenericMiot(MiotDevice):
    # we support all devices, if not, it is a responsibility of caller to verify that
    _supported_models = ["*"]

    _meta = Metadata.load()

    def __init__(
        self,
        ip: Optional[str] = None,
        token: Optional[str] = None,
        start_id: int = 0,
        debug: int = 0,
        lazy_discover: bool = True,
        timeout: Optional[int] = None,
        *,
        model: Optional[str] = None,
        mapping: Optional[MiotMapping] = None,
    ):
        super().__init__(
            ip,
            token,
            start_id,
            debug,
            lazy_discover,
            timeout,
            model=model,
            mapping=mapping,
        )
        self._model = model
        self._miot_model: Optional[DeviceModel] = None

        self._actions: dict[str, ActionDescriptor] = {}
        self._properties: dict[str, PropertyDescriptor] = {}
        self._status_query: list[dict] = []

    def initialize_model(self):
        """Initialize the miot model and create descriptions."""
        if self._miot_model is not None:
            return

        miotcloud = MiotCloud()
        self._miot_model = miotcloud.get_device_model(self.model)
        _LOGGER.debug("Initialized: %s", self._miot_model)
        self._create_descriptors()

    @command(
        click.option(
            "-v",
            "--verbose",
            is_flag=True,
            help="Output full property path for metadata ",
        ),
        default_output=format_output(result_msg_fmt=pretty_status),
    )
    def status(self, verbose=False) -> GenericMiotStatus:
        """Return status based on the miot model."""
        if not self._initialized:
            self._initialize_descriptors()

        # TODO: max properties needs to be made configurable (or at least splitted to avoid too large udp datagrams
        #       some devices are stricter: https://github.com/rytilahti/python-miio/issues/1550#issuecomment-1303046286
        response = self.get_properties(
            self._status_query, property_getter="get_properties", max_properties=10
        )

        return GenericMiotStatus(response, self)

    def get_extras(self, miot_entity):
        """Enriches descriptor with extra meta data from yaml definitions."""
        extras = miot_entity.extras
        extras["urn"] = miot_entity.urn
        extras["siid"] = miot_entity.siid

        # TODO: ugly way to detect the type
        if getattr(miot_entity, "aiid", None):
            extras["aiid"] = miot_entity.aiid
        if getattr(miot_entity, "piid", None):
            extras["piid"] = miot_entity.piid

        meta = self._meta.get_metadata(miot_entity)
        if meta:
            extras.update(meta)
        else:
            _LOGGER.warning(
                "Unable to find extras for %s %s",
                miot_entity.service,
                repr(miot_entity.urn),
            )

        return extras

    def _create_action(self, act: MiotAction) -> Optional[ActionDescriptor]:
        """Create action descriptor for miot action."""
        desc = act.get_descriptor()
        if act.inputs:
            # TODO: need to figure out how to expose input parameters for downstreams
            _LOGGER.warning(
                "Got inputs for action, skipping %s for %s", act, act.service
            )
            return None

        call_action = partial(self.call_action_by, act.siid, act.aiid)
        desc.method = call_action

        return desc

    def _create_actions(self, serv: MiotService):
        """Create action descriptors."""
        for act in serv.actions:
            act_desc = self._create_action(act)
            self.descriptors().add_descriptor(act_desc)

    def _create_properties(self, serv: MiotService):
        """Create sensor and setting descriptors for a service."""
        for prop in serv.properties:
            if prop.access == [MiotAccess.Notify]:
                _LOGGER.debug("Skipping notify-only property: %s", prop)
                continue
            if not prop.access:
                # some properties are defined only to be used as inputs or outputs for actions
                _LOGGER.debug(
                    "%s (%s) reported no access information",
                    prop.name,
                    prop.description,
                )
                continue

            desc = prop.get_descriptor()

            # Add readable properties to the status query
            if AccessFlags.Read in desc.access:
                extras = prop.extras
                prop = extras["miot_property"]
                q = {"siid": prop.siid, "piid": prop.piid, "did": prop.name}
                self._status_query.append(q)

            # Bind setter to the descriptor
            if AccessFlags.Write in desc.access:
                desc.setter = partial(
                    self.set_property_by, prop.siid, prop.piid, name=prop.name
                )

            self.descriptors().add_descriptor(desc)

    def _create_descriptors(self):
        """Create descriptors based on the miot model."""
        for serv in self._miot_model.services:
            if serv.siid == 1:
                continue  # Skip device details

            self._create_actions(serv)
            self._create_properties(serv)

        _LOGGER.debug("Created %s actions", len(self._actions))
        for act in self._actions.values():
            _LOGGER.debug(f"\t{act}")
        _LOGGER.debug("Created %s properties", len(self._properties))
        for sensor in self._properties.values():
            _LOGGER.debug(f"\t{sensor}")

    def _initialize_descriptors(self) -> None:
        """Initialize descriptors.

        This will be called by the base class to initialize the descriptors. We override
        it here to construct our model instead of trying to request  the status and use
        that to find out the available features.
        """
        self.initialize_model()
        self._initialized = True

    @property
    def device_type(self) -> Optional[str]:
        """Return device type."""
        # TODO: this should be probably mapped to an enum
        if self._miot_model is not None:
            return self._miot_model.urn.type
        return None

    @classmethod
    def get_device_group(cls):
        """Return device command group.

        TODO: insert the actions from the model for better click integration
        """
        return super().get_device_group()
