trigger SampleAccountTrigger on Account (before update) {
    SampleAction.beforeUpdate(Trigger.new);
}
