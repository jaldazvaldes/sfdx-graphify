// The fictional base is intentionally absent to exercise candidate call resolution.
public with sharing class SampleAccountHandler extends SampleExternalBase {
    public static void beforeUpdate(List<Account> records) {
        SampleAction.normalize(records);
    }
}
